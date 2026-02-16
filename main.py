from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pydantic import BaseModel
from bs4 import BeautifulSoup
import json
import re
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Float, DateTime, ForeignKey, select, Text
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import asyncio
import os
import sys

# Ensure asyncio on Windows uses the ProactorEventLoop so subprocesses (Playwright) work.
# Without this Playwright will raise NotImplementedError when it tries to spawn browsers.
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        # policy not available on very old Python versions — ignore
        pass

# Use SQLite by default, PostgreSQL if DATABASE_URL is set
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./db/backmarket.db")

# Ensure db directory exists
os.makedirs("db", exist_ok=True)

# --- logging setup -------------------------------------------------
import logging

# module logger used for retries, background tasks and errors
logger = logging.getLogger("backmarket")
logger.setLevel(logging.INFO)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(_stream_handler)

# Filter that suppresses uvicorn access log entries for HTTP GET requests
class _ExcludeGetFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if record.name.startswith("uvicorn.access") and (" GET " in msg or msg.strip().startswith("GET ")):
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(_ExcludeGetFilter())
# ------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    current_price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(10))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    seller: Mapped[str | None] = mapped_column(String(200))
    condition: Mapped[str | None] = mapped_column(String(100))
    warranty: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    price_history: Mapped[list["PriceHistory"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(10))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    product: Mapped["Product"] = relationship(back_populates="price_history")


engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def price_checker_task():
    """Background task to periodically check prices for all tracked products."""
    while True:
        await asyncio.sleep(3600)  # Check every hour
        try:
            async with async_session() as session:
                result = await session.execute(select(Product))
                products = result.scalars().all()

                for product in products:
                    try:
                        product_info = await scrape_backmarket_product(product.url, save_to_db=False)
                        if product_info.price:
                            new_price = float(product_info.price)
                            if product.current_price != new_price:
                                # Price changed, record it
                                price_record = PriceHistory(
                                    product_id=product.id,
                                    price=new_price,
                                    currency=product_info.currency
                                )
                                session.add(price_record)
                                product.current_price = new_price
                                product.updated_at = utc_now()
                        await asyncio.sleep(5)  # Rate limiting between requests
                    except Exception:
                        logger.exception("Error checking price for %s", product.url)
                        continue

                await session.commit()
        except Exception:
            logger.exception("Price checker error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(price_checker_task())
    yield
    task.cancel()


app = FastAPI(title="BackMarket Product API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Public API with no authentication
    allow_credentials=False,  # No cookies/auth used; avoids browser tracking prevention
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# Custom middleware to ensure CORS headers on every response (safety net for Cloudflare)
class CORSHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

app.add_middleware(CORSHeaderMiddleware)


class ProductInfo(BaseModel):
    title: str | None = None
    price: str | None = None
    currency: str | None = None
    condition: str | None = None
    image_url: str | None = None
    description: str | None = None
    seller: str | None = None
    warranty: str | None = None
    url: str
    refreshing: bool = False


async def scrape_backmarket_product(
    url: str,
    save_to_db: bool = True,
    wait_for_price_and_title: bool = False,
    max_attempts: int | None = None,
    retry_delay: float = 3.0,
    allow_stylesheets: bool = False,
) -> ProductInfo:
    """Scrape product information from a BackMarket product page.

    If `wait_for_price_and_title` is True the function will retry the whole
    fetch+parse loop until both `title` and `price` are present (or until
    `max_attempts` is reached). This is used by the `/get` endpoint for new
    products so callers can block until useful data is available.
    """

    if "backmarket." not in url:
        raise HTTPException(status_code=400, detail="URL must be a BackMarket product URL")

    attempts = 0
    html = None
    rendered_price_text: str | None = None

    while True:
        attempts += 1

        # 1) Fast HTTP fetch first (no subprocesses) — covers most pages that include structured data/meta tags
        try:
            import httpx
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-GB,en;q=0.9",
            }
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.text:
                    # quick pre-parse to determine if this HTML already contains usable data
                    quick_soup = BeautifulSoup(resp.text, "html.parser")
                    quick_title = False
                    quick_price = False

                    # JSON-LD quick check
                    _json_ld = quick_soup.find("script", type="application/ld+json")
                    if _json_ld:
                        try:
                            _data = json.loads(_json_ld.string or "{}")
                            if isinstance(_data, list):
                                for item in _data:
                                    if item.get("@type") == "Product":
                                        _data = item
                                        break
                            if _data.get("@type") == "Product":
                                quick_title = bool(_data.get("name"))
                                offers = _data.get("offers") or {}
                                if isinstance(offers, list) and offers:
                                    offers = offers[0]
                                quick_price = bool(offers.get("price"))
                        except Exception:
                            pass

                    # fallback quick checks
                    if not quick_title:
                        quick_title = bool(quick_soup.find("h1"))
                    if not quick_price:
                        if quick_soup.find("meta", property="product:price:amount"):
                            quick_price = True
                        elif quick_soup.find(attrs={"itemprop": "price"}):
                            quick_price = True

                    if quick_title or quick_price:
                        html = resp.text
        except Exception:
            # ignore HTTP errors — we'll try Playwright below if necessary
            html = html

        # 2) If HTTP fetch didn't return usable HTML (or page requires JS), fall back to Playwright
        if not html:
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1920, "height": 1080},
                        locale="en-GB",
                    )
                    page = await context.new_page()
                    # set reasonable timeouts for this page
                    page.set_default_navigation_timeout(60000)
                    page.set_default_timeout(60000)

                    # Block large/unnecessary resources (images/fonts/analytics) to speed navigation
                    async def _block_resource(route, request):
                        # allow opt-in to load stylesheets when necessary (helps pages that rely on CSS-driven rendering)
                        blocked = ("image", "media", "font")
                        if not allow_stylesheets:
                            blocked = blocked + ("stylesheet",)
                        if request.resource_type in blocked:
                            await route.abort()
                        else:
                            await route.continue_()

                    await context.route("**/*", _block_resource)

                    # Navigate using DOMContentLoaded (avoids indefinite network activity from analytics)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        # second attempt with a longer timeout before giving up
                        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

                    # Prefer waiting for JSON-LD *or* any price-related selector (meta, itemprop, visible price classes).
                    # This increases the chance we capture JS-updated prices without waiting for full networkidle.
                    try:
                        await page.wait_for_selector(
                            'script[type="application/ld+json"], meta[property="product:price:amount"], [itemprop="price"], [data-testid*="price"], [data-qa*="price"], [data-test*="price"], [data-test-id*="price"], .price, .price-amount, .product-price, span[class*=price]',
                            timeout=12000,
                        )
                    except Exception:
                        # best-effort short pause if nothing matched
                        await page.wait_for_timeout(1500)

                    # Small extra delay to let client-side hydration update price DOM if necessary
                    await page.wait_for_timeout(300)

                    # Best-effort: wait a short time for any visible currency/price text to appear
                    try:
                        await page.wait_for_function(
                            "!!(document.body && /[£€$]\\s*[\\d,\\.]+|\\b[\\d\\.,]+\\s*(?:GBP|EUR|USD)\\b/.test(document.body.innerText))",
                            timeout=3000,
                        )
                    except Exception:
                        # not critical — continue and try to extract from HTML/JSON
                        pass

                    # Safely retrieve the page HTML; if navigation started again retry once.
                    try:
                        html = await page.content()
                    except Exception as inner_e:
                        # transient navigation issue — short pause and retry
                        await page.wait_for_timeout(800)
                        html = await page.content()

                    # Try to capture rendered price text directly from the client DOM (helps complex SPA renderings)
                    try:
                        playwright_price = await page.evaluate(r"""
                            (() => {
                                const selectors = [
                                    '[data-testid*="price"]', '[data-qa*="price"]', '[data-test*="price"]',
                                    '[data-test-id*="price"]', '.price-amount', '.product-price', '.product-pricing',
                                    '.price', 'span[class*=price]', '.price-amount__integer', '.price-amount__fraction'
                                ];

                                for (const sel of selectors) {
                                    const el = document.querySelector(sel);
                                    if (el && el.innerText && /\d/.test(el.innerText)) {
                                        return el.innerText.trim();
                                    }
                                }

                                // common split-price pattern (integer + fraction)
                                const intEl = document.querySelector('.price-amount__integer, [data-qa="price-int"], .price-int');
                                const decEl = document.querySelector('.price-amount__fraction, [data-qa="price-decimal"], .price-decimal');
                                if (intEl) {
                                    const i = (intEl.innerText || '').replace(/[^\d]/g, '');
                                    const d = decEl ? ('.' + (decEl.innerText || '').replace(/[^\d]/g, '')) : '';
                                    return (i ? i + d : null);
                                }

                                return null;
                            })()
                        """)
                        if playwright_price:
                            rendered_price_text = playwright_price
                            logger.info("Playwright extracted rendered price text: %s", rendered_price_text)
                    except Exception:
                        # non-fatal — parsing will continue via BeautifulSoup fallbacks
                        rendered_price_text = rendered_price_text

                    # If Playwright didn't find a price and stylesheets were blocked, retry once with stylesheets enabled
                    if not rendered_price_text and not allow_stylesheets:
                        try:
                            logger.info("Retrying Playwright with stylesheets enabled for %s", url)
                            # create a second context that allows stylesheets (only block images/fonts)
                            second_ctx = await browser.new_context(
                                user_agent=(
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                                ),
                                viewport={"width": 1920, "height": 1080},
                                locale="en-GB",
                            )
                            page2 = await second_ctx.new_page()
                            page2.set_default_navigation_timeout(60000)
                            page2.set_default_timeout(60000)

                            async def _allow_styles_route(route, request):
                                if request.resource_type in ("image", "media", "font"):
                                    await route.abort()
                                else:
                                    await route.continue_()

                            await second_ctx.route("**/*", _allow_styles_route)

                            try:
                                await page2.goto(url, wait_until="domcontentloaded", timeout=45000)
                            except Exception:
                                await page2.goto(url, wait_until="domcontentloaded", timeout=90000)

                            await page2.wait_for_timeout(800)

                            try:
                                playwright_price2 = await page2.evaluate(r"""
                                    (() => {
                                        const selectors = [
                                            '[data-testid*="price"]', '[data-qa*="price"]', '[data-test*="price"]',
                                            '[data-test-id*="price"]', '.price-amount', '.product-price', '.product-pricing',
                                            '.price', 'span[class*=price]', '.price-amount__integer', '.price-amount__fraction'
                                        ];

                                        for (const sel of selectors) {
                                            const el = document.querySelector(sel);
                                            if (el && el.innerText && /\d/.test(el.innerText)) {
                                                return el.innerText.trim();
                                            }
                                        }

                                        const intEl = document.querySelector('.price-amount__integer, [data-qa="price-int"], .price-int');
                                        const decEl = document.querySelector('.price-amount__fraction, [data-qa="price-decimal"], .price-decimal');
                                        if (intEl) {
                                            const i = (intEl.innerText || '').replace(/[^\d]/g, '');
                                            const d = decEl ? ('.' + (decEl.innerText || '').replace(/[^\d]/g, '')) : '';
                                            return (i ? i + d : null);
                                        }

                                        return null;
                                    })()
                                """)
                                if playwright_price2:
                                    rendered_price_text = playwright_price2
                                    logger.info("Playwright (styles) extracted rendered price text: %s", rendered_price_text)
                                    
                                    # Only use the stylesheet HTML if we successfully found price data
                                    try:
                                        html2 = await page2.content()
                                        # Quick sanity check: does new HTML have a title?
                                        if html2 and '<title>' in html2:
                                            html = html2
                                            logger.info("Using stylesheet-enabled HTML (contains title and price)")
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                            await second_ctx.close()
                        except Exception:
                            logger.exception("Playwright stylesheet retry failed for %s", url)

                    # ensure browser is closed even on intermediate errors
                    await browser.close()
            except NotImplementedError as e:
                # Playwright can't spawn subprocesses in this environment (common on some Windows setups).
                logger.warning("Playwright subprocess unavailable: %s", e)

                # If the caller expects to *wait* for JS-rendered price/title, surface a clear 503 immediately
                if wait_for_price_and_title and not html:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Playwright is not available in this environment; JS-rendered price/title cannot be retrieved. "
                            "Run the app where Playwright can spawn browsers or provide a product URL that exposes structured data/meta tags."
                        ),
                    )

                # Otherwise (background jobs / manual checks) if we have no HTTP HTML, return an error so caller can handle/log it
                if not html:
                    raise HTTPException(status_code=500, detail="Playwright unavailable and HTTP fetch returned no content")

                # if we do have HTML from the HTTP-only fetch, continue to parse that content (no-op here)
            except Exception as e:
                # If caller requested continuous retries, swallow transient errors
                if wait_for_price_and_title:
                    logger.warning(
                        "Scrape attempt %d failed: %s — retrying in %.1fs",
                        attempts,
                        str(e),
                        retry_delay,
                    )
                    if max_attempts and attempts >= max_attempts:
                        raise HTTPException(status_code=500, detail=f"Failed after {attempts} attempts: {e}")
                    await asyncio.sleep(retry_delay)
                    continue
                raise HTTPException(status_code=500, detail=f"Failed to fetch URL: {str(e)}")

        # Parse HTML
        soup = BeautifulSoup(html, "html.parser")
        product_info = ProductInfo(url=url)

        # Try to extract data from JSON-LD structured data
        json_ld_script = soup.find("script", type="application/ld+json")
        if json_ld_script:
            try:
                json_data = json.loads(json_ld_script.string)
                if isinstance(json_data, list):
                    for item in json_data:
                        if item.get("@type") == "Product":
                            json_data = item
                            break

                if json_data.get("@type") == "Product":
                    product_info.title = json_data.get("name")
                    product_info.description = json_data.get("description")
                    product_info.image_url = json_data.get("image")

                    offers = json_data.get("offers", {})
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if offers:
                        product_info.price = str(offers.get("price"))
                        product_info.currency = offers.get("priceCurrency")

                    brand = json_data.get("brand", {})
                    if isinstance(brand, dict):
                        product_info.seller = brand.get("name")
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: Extract from HTML elements
        if not product_info.title:
            title_elem = soup.find("h1")
            if title_elem:
                product_info.title = title_elem.get_text(strip=True)

        # Additional: parse Next.js / inline JSON blobs and scan data-* attributes for price information
        if not product_info.price:
            # recursive search helper for JSON blobs
            def _search_json_for_price(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if re.search(r'price|amount|offers', k, re.I):
                            if isinstance(v, (int, float, str)) and re.search(r'\d', str(v)):
                                return str(v)
                        found = _search_json_for_price(v)
                        if found:
                            return found
                elif isinstance(obj, list):
                    for item in obj:
                        found = _search_json_for_price(item)
                        if found:
                            return found
                return None

            # 1) Look for __NEXT_DATA__ / application/json blobs (common in SSR/Next.js apps)
            next_data_script = soup.find("script", id="__NEXT_DATA__", type="application/json")
            if not next_data_script:
                # fallback to any JSON script blocks used by some apps
                next_data_script = soup.find("script", type="application/json")

            if next_data_script:
                try:
                    nd = json.loads(next_data_script.string or "{}")
                    found = _search_json_for_price(nd)
                    if found:
                        product_info.price = found.replace(",", "")

                        # try to find explicit currency nearby in the same JSON
                        def _search_json_for_currency(obj):
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    if re.search(r'priceCurrency', k, re.I) and isinstance(v, str) and v:
                                        return v
                                    res = _search_json_for_currency(v)
                                    if res:
                                        return res
                            elif isinstance(obj, list):
                                for item in obj:
                                    res = _search_json_for_currency(item)
                                    if res:
                                        return res
                            return None

                        cur = _search_json_for_currency(nd)
                        if cur:
                            if cur in ("GBP", "EUR", "USD"):
                                product_info.currency = product_info.currency or cur
                            elif cur == '£':
                                product_info.currency = product_info.currency or 'GBP'
                            elif cur == '€':
                                product_info.currency = product_info.currency or 'EUR'
                            elif cur == '$':
                                product_info.currency = product_info.currency or 'USD'

                        logger.info("Found price in JSON blob: %s %s", product_info.price, product_info.currency)
                except Exception:
                    pass

            # 2) Scan attributes (data-price, data-amount, aria-labels etc.) for values containing a numeric price
            if not product_info.price:
                for tag in soup.find_all():
                    for attr_name, attr_val in tag.attrs.items():
                        if re.search(r'price', attr_name, re.I):
                            av = attr_val
                            if isinstance(av, (list, tuple)):
                                av = " ".join(map(str, av))
                            m = re.search(r'[\d\.,]+', str(av))
                            if m:
                                product_info.price = m.group(0).replace(",", "")
                                if '€' in str(av):
                                    product_info.currency = product_info.currency or 'EUR'
                                elif '£' in str(av):
                                    product_info.currency = product_info.currency or 'GBP'
                                elif '$' in str(av):
                                    product_info.currency = product_info.currency or 'USD'
                                logger.info("Found price in element attribute %s: %s", attr_name, product_info.price)
                                break
                    if product_info.price:
                        break

        # Try to find price from meta tags
        if not product_info.price:
            price_meta = soup.find("meta", property="product:price:amount")
            if price_meta:
                product_info.price = price_meta.get("content")
            currency_meta = soup.find("meta", property="product:price:currency")
            if currency_meta:
                product_info.currency = currency_meta.get("content")

        # Additional fallback: look for visible price text, itemprop attributes, inline script JSON, or page text
        if not product_info.price:
            # 1) Visible elements (class/itemprop)
            price_item = soup.find(attrs={"itemprop": "price"}) or soup.find(class_=re.compile(r"price", re.I))
            if price_item:
                text = price_item.get_text(" ", strip=True)
                m = re.search(r"([£€$]\s?[\d\.,]+)|([\d\.,]+\s?(?:GBP|EUR|USD|£|€|\$))", text)
                if m:
                    extracted = m.group(0)
                    num_match = re.search(r"[\d\.,]+", extracted)
                    if num_match:
                        product_info.price = num_match.group(0).replace(",", "")
                    if '£' in extracted or 'GBP' in extracted:
                        product_info.currency = product_info.currency or "GBP"
                    elif '€' in extracted or 'EUR' in extracted:
                        product_info.currency = product_info.currency or "EUR"
                    elif '$' in extracted or 'USD' in extracted:
                        product_info.currency = product_info.currency or "USD"

            # 2) Scan inline <script> blocks for JSON-like price keys (common when site injects data)
            if not product_info.price:
                for script in soup.find_all("script"):
                    s = script.string or script.get_text()
                    if not s:
                        continue
                    # avoid huge blobs
                    if len(s) > 200000:
                        continue

                    # Try common JSON patterns: "price": "1234"  or  "amount": 1234
                    m1 = re.search(r'"price"\s*:\s*"?([\d\.,]+)"?', s)
                    m2 = re.search(r'"amount"\s*:\s*([\d\.]+)', s)
                    # also allow single-quoted or unquoted price keys
                    m3 = re.search(r"(?:'price'|price)\s*:\s*'?([\d\.,]+)'?", s)
                    m4 = re.search(r"(?:'amount'|amount)\s*:\s*([\d\.]+)", s)
                    if m1:
                        product_info.price = m1.group(1).replace(",", "")
                    elif m2:
                        product_info.price = m2.group(1)
                    elif m3:
                        product_info.price = m3.group(1).replace(",", "")
                    elif m4:
                        product_info.price = m4.group(1)

                    if product_info.price:
                        # try to extract currency nearby
                        c = re.search(r'"priceCurrency"\s*:\s*"?([A-Z]{3}|[£€$])"?', s) or re.search(r"(?:'priceCurrency'|priceCurrency)\s*:\s*'?([A-Z]{3}|[£€$])'?", s)
                        if c:
                            cur = c.group(1)
                            if cur in ("GBP", "EUR", "USD"):
                                product_info.currency = product_info.currency or cur
                            elif cur == '£':
                                product_info.currency = product_info.currency or 'GBP'
                            elif cur == '€':
                                product_info.currency = product_info.currency or 'EUR'
                            elif cur == '$':
                                product_info.currency = product_info.currency or 'USD'
                        logger.info("Found price in inline script: %s %s", product_info.price, product_info.currency)
                        break

            # 3) If still not found, use Playwright-evaluated rendered text (helps SPA-only renderings)
            if not product_info.price and rendered_price_text:
                rp = rendered_price_text
                m = re.search(r"([£€$]\s?[\d\.,]+)|([\d\.,]+\s?(?:GBP|EUR|USD|£|€|\$))|([\d\.,]+)", rp)
                if m:
                    extracted = m.group(1) or m.group(2) or m.group(3)
                    num_match = re.search(r"[\d\.,]+", extracted)
                    if num_match:
                        product_info.price = num_match.group(0).replace(",", "")
                        if '£' in extracted or 'GBP' in extracted:
                            product_info.currency = product_info.currency or "GBP"
                        elif '€' in extracted or 'EUR' in extracted:
                            product_info.currency = product_info.currency or "EUR"
                        elif '$' in extracted or 'USD' in extracted:
                            product_info.currency = product_info.currency or "USD"
                        logger.info("Found price from Playwright DOM-eval: %s %s", product_info.price, product_info.currency)

            # 4) Last resort: search whole page text for currency followed by number
            if not product_info.price:
                text = soup.get_text()
                m = re.search(r"([£€$]\s?[\d\.,]+)|([\d\.,]+\s?(?:GBP|EUR|USD))", text)
                if m:
                    extracted = m.group(0)
                    num_match = re.search(r"[\d\.,]+", extracted)
                    if num_match:
                        product_info.price = num_match.group(0).replace(",", "")

        # If caller requires both title and price, retry until both are present
        if wait_for_price_and_title and (not product_info.price or not product_info.title):
            logger.info(
                "Scrape attempt %d returned incomplete data (title=%s, price=%s). Retrying in %.1fs...",
                attempts,
                bool(product_info.title),
                bool(product_info.price),
                retry_delay,
            )
            if max_attempts and attempts >= max_attempts:
                raise HTTPException(status_code=500, detail=f"Failed to retrieve title and price after {attempts} attempts")
            await asyncio.sleep(retry_delay)
            continue

        # Save to database if enabled (unchanged behaviour)
        if save_to_db:
            async with async_session() as session:
                # Check if product already exists
                result = await session.execute(select(Product).where(Product.url == url))
                product = result.scalar_one_or_none()

                price_float = float(product_info.price) if product_info.price else None

                if product:
                    # Update existing product
                    old_price = product.current_price
                    product.title = product_info.title
                    product.current_price = price_float
                    product.currency = product_info.currency
                    product.image_url = product_info.image_url
                    product.description = product_info.description
                    product.seller = product_info.seller
                    product.updated_at = utc_now()

                    # Record price change if different
                    if price_float and old_price != price_float:
                        price_record = PriceHistory(
                            product_id=product.id,
                            price=price_float,
                            currency=product_info.currency
                        )
                        session.add(price_record)
                else:
                    # Create new product
                    product = Product(
                        url=url,
                        title=product_info.title,
                        current_price=price_float,
                        currency=product_info.currency,
                        image_url=product_info.image_url,
                        description=product_info.description,
                        seller=product_info.seller,
                    )
                    session.add(product)
                    await session.flush()  # Get the product ID

                    # Record initial price
                    if price_float:
                        price_record = PriceHistory(
                            product_id=product.id,
                            price=price_float,
                            currency=product_info.currency
                        )
                        session.add(price_record)

                await session.commit()

        return product_info


async def background_scrape(url: str):
    """Scrape a product in the background and update the database."""
    try:
        await scrape_backmarket_product(url, save_to_db=True)
        logger.info("Background scrape completed for %s", url)
    except Exception:
        logger.exception("Background scrape failed for %s", url)


# Catch-all OPTIONS handler for CORS preflight requests
@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str, response: Response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


@app.get("/get", response_model=ProductInfo)
async def get_product_info(
    url: str = Query(..., description="BackMarket product URL"),
    allow_stylesheets: bool = Query(False, description="Allow loading stylesheets in Playwright (may slow scraping)")
):
    """
    Get information about a BackMarket product.
    If the product is already tracked, returns cached data immediately
    and triggers a background refresh. If new, scrapes synchronously.
    """
    if "backmarket." not in url:
        raise HTTPException(status_code=400, detail="URL must be a BackMarket product URL")
    
    # Check if product already exists in the database
    async with async_session() as session:
        result = await session.execute(select(Product).where(Product.url == url))
        product = result.scalar_one_or_none()
    
    if product:
        # Product exists — return cached data immediately and refresh in background
        asyncio.create_task(background_scrape(url))
        return ProductInfo(
            title=product.title,
            price=str(product.current_price) if product.current_price else None,
            currency=product.currency,
            image_url=product.image_url,
            description=product.description,
            seller=product.seller,
            condition=product.condition,
            warranty=product.warranty,
            url=product.url,
            refreshing=True
        )
    else:
        # New product — wait (up to 12 attempts, 3s interval) until price & title are available
        product_info = await scrape_backmarket_product(
            url,
            save_to_db=True,
            wait_for_price_and_title=True,
            max_attempts=12,
            retry_delay=3.0,
            allow_stylesheets=allow_stylesheets,
        )
        product_info.refreshing = False
        return product_info


class PricePoint(BaseModel):
    price: float
    currency: str | None
    recorded_at: datetime


class PriceHistoryResponse(BaseModel):
    url: str
    title: str | None
    current_price: float | None
    currency: str | None
    price_history: list[PricePoint]


@app.get("/get/history", response_model=PriceHistoryResponse)
async def get_price_history(url: str = Query(..., description="BackMarket product URL")):
    """Get price history data as JSON for a tracked product."""
    async with async_session() as session:
        result = await session.execute(
            select(Product).where(Product.url == url)
        )
        product = result.scalar_one_or_none()

        if not product:
            raise HTTPException(status_code=404, detail="Product not found. Request it first via /get?url=")

        # Get price history
        history_result = await session.execute(
            select(PriceHistory)
            .where(PriceHistory.product_id == product.id)
            .order_by(PriceHistory.recorded_at)
        )
        history = history_result.scalars().all()

        return PriceHistoryResponse(
            url=product.url,
            title=product.title,
            current_price=product.current_price,
            currency=product.currency,
            price_history=[
                PricePoint(price=h.price, currency=h.currency, recorded_at=h.recorded_at)
                for h in history
            ]
        )


@app.get("/get/charts", response_class=HTMLResponse)
async def get_price_chart(url: str = Query(..., description="BackMarket product URL")):
    """Get a visual price history chart for a tracked product."""
    async with async_session() as session:
        result = await session.execute(
            select(Product).where(Product.url == url)
        )
        product = result.scalar_one_or_none()

        if not product:
            raise HTTPException(status_code=404, detail="Product not found. Request it first via /get?url=")

        # Get price history
        history_result = await session.execute(
            select(PriceHistory)
            .where(PriceHistory.product_id == product.id)
            .order_by(PriceHistory.recorded_at)
        )
        history = history_result.scalars().all()

        if not history:
            raise HTTPException(status_code=404, detail="No price history available for this product")

        # Prepare data for chart
        dates = [h.recorded_at.isoformat() for h in history]
        prices = [h.price for h in history]
        currency = product.currency or "GBP"

        min_price = min(prices)
        max_price = max(prices)
        current_price = product.current_price or prices[-1]

        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Price History - {product.title or 'Product'}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            padding: 20px;
            color: #fff;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .header h1 {{
            font-size: 1.8rem;
            margin-bottom: 10px;
            color: #4ade80;
        }}
        .header p {{
            color: #94a3b8;
            font-size: 0.9rem;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 20px;
            text-align: center;
            backdrop-filter: blur(10px);
        }}
        .stat-card .label {{
            font-size: 0.85rem;
            color: #94a3b8;
            margin-bottom: 8px;
        }}
        .stat-card .value {{
            font-size: 1.8rem;
            font-weight: bold;
        }}
        .stat-card.current .value {{ color: #4ade80; }}
        .stat-card.min .value {{ color: #22d3ee; }}
        .stat-card.max .value {{ color: #f87171; }}
        .chart-container {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 30px;
            backdrop-filter: blur(10px);
        }}
        .product-link {{
            display: block;
            text-align: center;
            margin-top: 20px;
            color: #4ade80;
            text-decoration: none;
        }}
        .product-link:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{product.title or 'Product Price History'}</h1>
            <p>Tracking since {history[0].recorded_at.strftime('%B %d, %Y')}</p>
        </div>
        
        <div class="stats">
            <div class="stat-card current">
                <div class="label">Current Price</div>
                <div class="value">{currency} {current_price:.2f}</div>
            </div>
            <div class="stat-card min">
                <div class="label">Lowest Price</div>
                <div class="value">{currency} {min_price:.2f}</div>
            </div>
            <div class="stat-card max">
                <div class="label">Highest Price</div>
                <div class="value">{currency} {max_price:.2f}</div>
            </div>
        </div>
        
        <div class="chart-container">
            <canvas id="priceChart"></canvas>
        </div>
        
        <a href="{product.url}" target="_blank" class="product-link">View on BackMarket →</a>
    </div>
    
    <script>
        const ctx = document.getElementById('priceChart').getContext('2d');
        const dates = {json.dumps(dates)};
        const prices = {json.dumps(prices)};
        
        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: dates,
                datasets: [{{
                    label: 'Price ({currency})',
                    data: prices,
                    borderColor: '#4ade80',
                    backgroundColor: 'rgba(74, 222, 128, 0.1)',
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4,
                    pointBackgroundColor: '#4ade80',
                    pointBorderColor: '#fff',
                    pointBorderWidth: 2,
                    pointRadius: 5,
                    pointHoverRadius: 8
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: true,
                plugins: {{
                    legend: {{
                        display: false
                    }},
                    tooltip: {{
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        titleColor: '#fff',
                        bodyColor: '#4ade80',
                        padding: 12,
                        displayColors: false,
                        callbacks: {{
                            label: function(context) {{
                                return '{currency} ' + context.parsed.y.toFixed(2);
                            }}
                        }}
                    }}
                }},
                scales: {{
                    x: {{
                        type: 'time',
                        time: {{
                            unit: 'day',
                            displayFormats: {{
                                day: 'MMM d, yyyy'
                            }}
                        }},
                        grid: {{
                            color: 'rgba(255, 255, 255, 0.1)'
                        }},
                        ticks: {{
                            color: '#94a3b8'
                        }}
                    }},
                    y: {{
                        grid: {{
                            color: 'rgba(255, 255, 255, 0.1)'
                        }},
                        ticks: {{
                            color: '#94a3b8',
                            callback: function(value) {{
                                return '{currency} ' + value;
                            }}
                        }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>
        """
        return HTMLResponse(content=html_content)


@app.get("/products")
async def list_tracked_products():
    """List all tracked products."""
    async with async_session() as session:
        result = await session.execute(select(Product).order_by(Product.updated_at.desc()))
        products = result.scalars().all()

        return [
            {
                "id": p.id,
                "url": p.url,
                "title": p.title,
                "current_price": p.current_price,
                "currency": p.currency,
                "image_url": p.image_url,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
            for p in products
        ]


@app.delete("/products/{product_id}")
async def delete_tracked_product(product_id: int):
    """
    Delete a tracked product (by ID) and its price history.
    The product row is removed and subsequent product listings will naturally "move up".
    """
    async with async_session() as session:
        product = await session.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        # Deleting the Product object will also remove related PriceHistory rows
        # because the relationship uses cascade="all, delete-orphan".
        await session.delete(product)
        await session.commit()

        # Return confirmation and remaining product count so clients can refresh/list again
        result = await session.execute(select(Product).order_by(Product.updated_at.desc()))
        remaining = result.scalars().all()
        return {
            "message": "Product deleted",
            "deleted_id": product_id,
            "remaining_count": len(remaining)
        }


@app.get("/check")
async def check_price(url: str = Query(..., description="Product URL to check")):
    """
    Manually trigger a price check for a specific tracked product.
    """
    async with async_session() as session:
        result = await session.execute(select(Product).where(Product.url == url))
        product = result.scalar_one_or_none()

        if not product:
            raise HTTPException(status_code=404, detail="Product not found. Request it first via /get?url=")

        try:
            product_info = await scrape_backmarket_product(product.url, save_to_db=False)
            old_price = product.current_price
            new_price = float(product_info.price) if product_info.price else None

            price_changed = False
            if new_price and old_price != new_price:
                # Price changed, record it
                price_record = PriceHistory(
                    product_id=product.id,
                    price=new_price,
                    currency=product_info.currency
                )
                session.add(price_record)
                product.current_price = new_price
                product.updated_at = utc_now()
                price_changed = True

            await session.commit()

            return {
                "url": product.url,
                "title": product.title,
                "old_price": old_price,
                "new_price": new_price,
                "currency": product_info.currency,
                "price_changed": price_changed,
                "status": "success"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error checking price: {str(e)}")


@app.get("/check-all")
async def check_all_prices():
    """
    Manually trigger a price check for all tracked products.
    """
    results = []

    async with async_session() as session:
        result = await session.execute(select(Product))
        products = result.scalars().all()

        if not products:
            return {"message": "No products to check", "results": []}

        for product in products:
            try:
                product_info = await scrape_backmarket_product(product.url, save_to_db=False)
                old_price = product.current_price
                new_price = float(product_info.price) if product_info.price else None

                price_changed = False
                if new_price and old_price != new_price:
                    # Price changed, record it
                    price_record = PriceHistory(
                        product_id=product.id,
                        price=new_price,
                        currency=product_info.currency
                    )
                    session.add(price_record)
                    product.current_price = new_price
                    product.updated_at = utc_now()
                    price_changed = True

                results.append({
                    "url": product.url,
                    "title": product.title,
                    "old_price": old_price,
                    "new_price": new_price,
                    "currency": product_info.currency,
                    "price_changed": price_changed,
                    "status": "success"
                })
            except Exception as e:
                results.append({
                    "url": product.url,
                    "title": product.title,
                    "status": "error",
                    "error": str(e)
                })

        await session.commit()

    changed_count = sum(1 for r in results if r.get("price_changed"))
    return {
        "message": f"Checked {len(results)} product(s), {changed_count} price change(s) detected",
        "results": results
    }


@app.get("/")
async def root():
    return {
        "message": "BackMarket Product API",
        "usage": "GET /get?url=<backmarket_product_url>",
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


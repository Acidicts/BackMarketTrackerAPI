from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pydantic import BaseModel
from bs4 import BeautifulSoup
import json
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Float, DateTime, ForeignKey, select, Text
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import asyncio
import os

# Use SQLite by default, PostgreSQL if DATABASE_URL is set
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./db/backmarket.db")

# Ensure db directory exists
os.makedirs("db", exist_ok=True)


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
                    except Exception as e:
                        print(f"Error checking price for {product.url}: {e}")
                        continue

                await session.commit()
        except Exception as e:
            print(f"Price checker error: {e}")


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


async def scrape_backmarket_product(url: str, save_to_db: bool = True) -> ProductInfo:
    """Scrape product information from a BackMarket product page."""

    if "backmarket." not in url:
        raise HTTPException(status_code=400, detail="URL must be a BackMarket product URL")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-GB",
            )
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)  # Wait for dynamic content

            html = await page.content()
            await browser.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch URL: {str(e)}")

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

    # Try to find price from meta tags
    if not product_info.price:
        price_meta = soup.find("meta", property="product:price:amount")
        if price_meta:
            product_info.price = price_meta.get("content")
        currency_meta = soup.find("meta", property="product:price:currency")
        if currency_meta:
            product_info.currency = currency_meta.get("content")

    # Try to find image from og:image
    if not product_info.image_url:
        og_image = soup.find("meta", property="og:image")
        if og_image:
            product_info.image_url = og_image.get("content")

    # Try to find description from meta
    if not product_info.description:
        desc_meta = soup.find("meta", attrs={"name": "description"})
        if desc_meta:
            product_info.description = desc_meta.get("content")

    # Save to database if enabled
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
        print(f"Background scrape completed for {url}")
    except Exception as e:
        print(f"Background scrape failed for {url}: {e}")


# Catch-all OPTIONS handler for CORS preflight requests
@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str, response: Response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


@app.get("/get", response_model=ProductInfo)
async def get_product_info(url: str = Query(..., description="BackMarket product URL")):
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
        # New product — must scrape synchronously to have any data
        product_info = await scrape_backmarket_product(url, save_to_db=True)
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


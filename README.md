# BackMarket Tracker API

A FastAPI-based service that tracks BackMarket product prices over time and provides price history charts.

## Features

- 🔍 Scrape BackMarket product information using Playwright
- 💾 Store product data and price history in a database (SQLite or PostgreSQL)
- 📊 View price history as interactive charts
- 🔄 Automatic hourly price checks for all tracked products
- 🐳 Docker support for easy deployment

## API Endpoints

### `GET /get?url=<product_url>`
Get information about a BackMarket product and start tracking it.

**Example:**
```bash
curl "http://localhost:8000/get?url=https://www.backmarket.co.uk/en-gb/p/samsung-galaxy-s24-ultra-256-gb-black-unlocked/..."
```

### `GET /get/history?url=<product_url>`
Get price history data as JSON for a tracked product.

### `GET /get/charts?url=<product_url>`
Get a visual price history chart for a tracked product.

### `GET /products`
List all tracked products.

### `GET /check?url=<product_url>`
Manually trigger a price check for a specific tracked product.

### `GET /check-all`
Manually trigger a price check for all tracked products.

### `DELETE /products/{product_id}`
Delete a tracked product and its price history.

## Deployment

### Using Docker

```bash
docker-compose up -d
```

The API will be available at `http://localhost:8000`.

### Using Docker with PostgreSQL

Edit `docker-compose.yml` and uncomment the PostgreSQL configuration, then:

```bash
docker-compose up -d
```

### Manual Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

2. Run the application:
```bash
python main.py
```

## Environment Variables

- `DATABASE_URL`: Database connection string (default: `sqlite+aiosqlite:///./db/backmarket.db`)
  - For PostgreSQL: `postgresql+asyncpg://user:password@host:port/dbname`

## Database

The application stores:
- **Products**: URL, title, current price, metadata
- **Price History**: Historical price changes with timestamps

Price checks run automatically every hour for all tracked products.

## Documentation

Interactive API documentation is available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Technology Stack

- **FastAPI**: Modern web framework for building APIs
- **Playwright**: Browser automation for scraping
- **SQLAlchemy**: Async ORM for database operations
- **BeautifulSoup**: HTML parsing
- **Chart.js**: Interactive price charts


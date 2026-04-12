# Zillow Scraper

Fast async Zillow property scraper with FastAPI server. Resolves addresses or
accepts URLs directly, fetches full listing data, and enriches each listing
with nearby homes from the same zipcode.

## What you get per listing

**Core property data** (extracted from `__NEXT_DATA__`):
- `zpid`, `url`, `full_address`, `street`, `city`, `state`, `zipcode`
- `price`, `price_str`, `zestimate`, `rent_zestimate`, `last_sold_price`, `last_sold_date`
- `bedrooms`, `bathrooms`, `living_area_sqft`, `lot_size_sqft`, `home_type`, `year_built`
- `price_per_sqft`, `property_tax_rate`, `monthly_hoa_fee`
- `parking_spaces`, `garage_spaces`, `parking_features`
- `listing_status`, `days_on_zillow`, `views`, `saves`, `listing_date`
- `agent_name`, `agent_phone`, `broker_name`, `mls_id`
- `mortgage_rate_30yr`, `mortgage_rate_15yr`, `mortgage_rate_arm5`
- `description`, `highlights`, `latitude`, `longitude`
- `photos` (up to 50 high-res URLs), `photo_count`

**Enrichment from zipcode pages** (cached, fetched once per unique zip):
- `nearby_homes` — up to 20 listings in the same zipcode with `price`, `bedrooms`,
  `bathrooms`, `living_area`, `address`, `url`, `image`

**Conditional** (present only for showcase listings):
- `schools`, `price_history`, `tax_history` — these are loaded via Zillow's
  `ForSaleNonPriorityQuery` GraphQL endpoint, which the BrightData web_unlocker
  zone blocks. Showcase listings have them embedded in the initial page load
  (~24% of listings in our test set).

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` with your BrightData credentials:

```env
BRIGHTDATA_CUSTOMER_ID=hl_xxxxxxxx
BRIGHTDATA_PASSWORD=xxxxxxxxxx
BRIGHTDATA_ZONE=zillow_web_unlocker
BRIGHTDATA_HOST=brd.superproxy.io
BRIGHTDATA_PORT=33335
BRIGHTDATA_COUNTRY=us
```

## Run the server

```bash
python run.py                # http://0.0.0.0:8000
python run.py --port 3000    # custom port
python run.py --reload       # auto-reload on code change
```

The server returns results directly in the HTTP response — it does **not**
write any output files. Files are only written when you run the test scripts
locally (see below).

## Endpoints

| Endpoint | Input | Returns |
|----------|-------|---------|
| `POST /scrape/urls` | Zillow listing URLs | **Full data + nearby homes** |
| `POST /scrape/addresses` | Street addresses | **Full data + nearby homes** |
| `POST /scrape/urls/fast` | Zillow listing URLs | Core data only (no nearby) |
| `POST /scrape` | Addresses (SSE) | Streaming, core data only |
| `POST /scrape/sync` | Addresses | Sync, core data only |
| `GET  /health` | — | Health check |
| `GET  /docs` | — | Swagger UI |

### `POST /scrape/urls` (recommended for URLs)

```bash
curl -X POST http://localhost:8000/scrape/urls \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://www.zillow.com/homedetails/9315-Parrot-Ave-Downey-CA-90240/21091004_zpid/"
    ],
    "concurrency": 100,
    "retries": 2
  }'
```

### `POST /scrape/addresses` (recommended for addresses)

```bash
curl -X POST http://localhost:8000/scrape/addresses \
  -H "Content-Type: application/json" \
  -d '{
    "addresses": [
      "9315 Parrot Ave, Downey, CA 90240",
      "1432 Kellam Ave, Los Angeles, CA 90026"
    ],
    "concurrency": 100,
    "retries": 2
  }'
```

### Response format

```json
{
  "results": [
    {
      "zpid": "21091004",
      "full_address": "9315 Parrot Ave, Downey, CA 90240",
      "price_str": "$1,150,000",
      "bedrooms": 4,
      "bathrooms": 3,
      "living_area_sqft": 2169,
      "year_built": 1950,
      "price_per_sqft": 530,
      "nearby_homes": [
        { "street": "9570 Gallatin Rd", "price_str": "$1,675,000", "bedrooms": 3, ... }
      ]
    }
  ],
  "summary": {
    "total": 100,
    "success": 100,
    "with_nearby_homes": 100,
    "elapsed": 37.8,
    "rate": 2.64
  }
}
```

## Test scripts

Two test runners are included. They write the same files committed to the repo
so you can verify reproducibility.

```bash
# Test URL endpoint with 5 sample URLs → property_details_100.json
python test_urls.py urls

# Test address endpoint with first 100 entries from input.json
# → property_details_addresses_100.json
python test_urls.py addresses
```

Note: Only the test scripts write JSON files. The server endpoints return
results in the HTTP response body — no files are written when you call the API.

## Performance benchmarks

Measured against `input.json` (100 items, BrightData web_unlocker zone, concurrency=100):

| Endpoint | Items | Time | Success | Nearby Homes | Rate |
|----------|-------|------|---------|--------------|------|
| `/scrape/urls`      | 100 URLs      | 37.8s | 100/100 | 100/100 | 2.64/s |
| `/scrape/addresses` | 100 addresses | 31.7s | 71/100  | 71/71   | 3.16/s |

The address endpoint is faster because the autocomplete ZPID lookup uses a
direct (non-proxy) connection. Address resolution success rate (~71%) is bounded
by Zillow autocomplete's fuzzy matching — addresses without an active listing
won't resolve.

### Why not faster?

The hard limit is BrightData web_unlocker's per-customer rate cap (~1.6–2.6
req/sec). For 100 listings, the scraper makes ~175 requests (100 listings + ~75
unique zipcode pages). Both phases overlap aggressively and zipcode pages are
deduplicated, so this is near the theoretical floor for the current proxy tier.

To push under 30s for 100 listings with full data, switch the BrightData zone
to `Datacenter` or `Residential` (higher concurrent connection limits). The
scraper code itself is already as parallel as possible.

## Project layout

```
zillow_scraper/
├── app/
│   ├── __init__.py
│   ├── config.py         # Settings + env loading
│   ├── models.py         # PropertyListing dataclass + Pydantic request models
│   ├── proxy.py          # BrightData proxy URL builder
│   ├── scraper.py        # Core scraping logic
│   └── server.py         # FastAPI app + endpoints
├── input.json            # 1000 sample addresses for testing
├── property_details_100.json            # Output of `test_urls.py urls`
├── property_details_addresses_100.json  # Output of `test_urls.py addresses`
├── test_urls.py          # Test runner
├── run.py                # Server entry point
├── requirements.txt
└── README.md
```

## Notes on missing fields

- `year_built`, `price_per_sqft`, `living_area_sqft` — populate for ~94–98% of
  listings. Missing values are typically apartments/rentals/vacant land.
- `schools`, `price_history`, `tax_history` — only populate for ~24% of
  listings (showcase listings). The remaining ~76% load this data via a
  separate GraphQL call that BrightData's web_unlocker zone blocks at the
  proxy. This is a product limitation, not a bug in the scraper.
- `nearby_homes` — populates for 100% of successful listings. Each listing's
  zipcode page is fetched once and cached, so listings in the same zipcode
  share the result.

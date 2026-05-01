# Zillow PX-Bypass Scraper

High-throughput Zillow scraper that bypasses PerimeterX (HUMAN Bot Defender)
press-and-hold protection without paying for a captcha solver. Uses a patched
Chromium to mint wide-scope `_px3` cookies through an Evomi residential proxy,
then replays the cookies via concurrent `httpx` against Zillow's
`ForSaleNonPriorityQuery` GraphQL endpoint.

**Validated benchmark:** 2500 URLs in 119.8s — 100% success — 20.88 listings/sec.

## Architecture

```
                     ┌─────────────────────────┐
                     │  patchright + Evomi     │  free, ~17s/mint
                     │  (mints _px3 cookies)   │  no captcha service
                     └────────────┬────────────┘
                                  │
                          ┌───────▼────────┐
                          │  CookiePool    │  N slots, sticky proxy
                          │  per slot      │  per-slot concurrency cap
                          └───────┬────────┘     auto-refresh on 403
                                  │
                ┌─────────────────▼──────────────────┐
                │  scrape_urls_via_px (httpx + h2)   │  ~21 listings/sec
                │  ForSaleNonPriorityQuery GraphQL   │  full property data
                └────────────────────────────────────┘
```

### Why each piece exists

- **patchright** — a maintained patched Playwright that hides the automation
  flags vanilla Playwright/Camoufox leak. PX accepts its fingerprint, so a
  fresh `_px3` cookie is minted on a normal page load (no press-and-hold
  challenge needed in our testing). Free, no per-mint cost.

- **Evomi residential proxy** — every request (mint + replay) routes through
  a residential exit IP. Sticky session per cookie slot, so PX sees one IP
  per session token instead of churn across ASNs.

- **Cookie pool** — PX flags a `_px3` token after a few hundred hits. We mint
  N cookies up front, round-robin them, and on 403 mark the slot dead and
  schedule a background mint. Per-slot concurrency cap prevents bursts from
  burning a cookie prematurely.

- **`ForSaleNonPriorityQuery`** — Zillow's GraphQL endpoint for full listing
  detail (schools, price history, tax history, nearby homes). Body must be a
  byte-exact match against Zillow's safelist; the verbatim 34 KB query is
  in `px_bypass/queries/for_sale_non_priority.json`.

## Setup

```bash
pip install -r requirements.txt
patchright install chrome
```

Create `.env` in the project root:

```bash
EVOMI_USERNAME=...
EVOMI_PASSWORD=...
EVOMI_HOST=rp.evomi.com    # default
EVOMI_PORT=1000            # default
```

## Run

### HTTP API server

```bash
python run.py                          # http://0.0.0.0:8000, headless
python run.py --port 3000              # custom port
python run.py --headed                 # show the patched Chromium (debug)
python run.py --pool-size 12           # bigger cookie pool
python run.py --reload                 # auto-reload on code change
```

`run.py` is the single entry point. On startup it boots `px_bypass.serve:app`
which launches one patched Chromium, mints `--pool-size` cookies in parallel
via Evomi (~50s for 8 cookies), then serves the API.

### Bulk CLI test

```bash
python -m px_bypass.test_2500 100 30 4 --proxy-only       # smoke test
python -m px_bypass.test_2500 2500 60 8 --proxy-only      # validation run
# args: <n_urls> <concurrency> <pool_size>
```

Reads URLs from `input-urls.json`, writes results to
`property_details_pxbypass_2500.json`.

## API

| Endpoint | Input | Notes |
|---|---|---|
| `POST /v2/scrape/urls` | Zillow listing URLs | Direct scrape via cookie pool |
| `POST /v2/scrape/addresses` | Street addresses | Resolves via Zillow autocomplete (free, no proxy) → scrapes |
| `GET /health` | — | Pool stats |

### `POST /v2/scrape/urls`

```bash
curl -X POST http://localhost:8000/v2/scrape/urls \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://www.zillow.com/homedetails/.../21091004_zpid/"],
    "concurrency": 60,
    "max_retries": 3
  }'
```

### `POST /v2/scrape/addresses`

```bash
curl -X POST http://localhost:8000/v2/scrape/addresses \
  -H "Content-Type: application/json" \
  -d '{
    "addresses": ["9315 Parrot Ave, Downey, CA 90240"],
    "concurrency": 60,
    "resolve_concurrency": 50
  }'
```

Response:

```json
{
  "results": [{
    "zpid": "21091004",
    "full_address": "9315 Parrot Ave, Downey, CA 90240",
    "price_str": "$1,150,000",
    "bedrooms": 4, "bathrooms": 3, "living_area_sqft": 2169,
    "schools": [...], "price_history": [...], "tax_history": [...],
    "nearby_homes": [...]
  }],
  "summary": {
    "total": 1, "success": 1, "blocked": 0,
    "elapsed_s": 0.85, "rate_per_sec": 1.18,
    "pool": {"size": 8, "live": 8, "mints": 8, "refreshes": 0}
  }
}
```

### `GET /health`

```json
{"status": "ok", "size": 8, "live": 8, "mints": 8, "refreshes": 0}
```

## Tuning

| Variable | Default | Effect |
|---|---|---|
| `POOL_SIZE` | 8 | Cookies in rotation. Bigger = more throughput, more Evomi bandwidth on init. |
| `POOL_PER_SLOT_C` | 5 | Concurrent requests per cookie. Higher kills cookies faster; 5 is the sweet spot. |
| `concurrency` (request) | 60 | Total in-flight requests. Effective ceiling = `POOL_SIZE × POOL_PER_SLOT_C`. |
| `HEADLESS` | true | Set `false` on a desktop to watch the minter solve the challenge. |

## Cost (at 50k listings/day)

| | Per month |
|---|---|
| patchright cookie minting | $0 |
| Evomi residential bandwidth | ~$55 |
| Lightsail VM ($5 plan) | $5 |
| **Total** | **~$60/mo** |

vs ~$5,000/mo for a BrightData web_unlocker zone at the same volume.

## Deployment (Lightsail)

The VM is headless, so patchright needs `xvfb` to run a real X display:

```bash
sudo apt install -y xvfb
xvfb-run -a python -m px_bypass.serve
```

Or use the included `systemd-zillow.service` unit (edit paths/user, then
`sudo systemctl enable --now zillow`).

## Project layout

```
zillow_scraper/
├── px_bypass/
│   ├── __init__.py
│   ├── cookie_pool.py        # rotation, sticky proxy, auto-refresh on 403
│   ├── parser.py             # GraphQL response → PropertyListing
│   ├── patchright_minter.py  # patched Chromium → _px3 cookies
│   ├── proxy.py              # Evomi sticky-session URL builder
│   ├── queries/
│   │   └── for_sale_non_priority.json   # verbatim Zillow GraphQL body
│   ├── scraper.py            # concurrent httpx replay + retry/halt logic
│   ├── serve.py              # FastAPI app + lifespan-managed pool
│   ├── systemd-zillow.service
│   └── test_2500.py          # bulk CLI test runner
├── app/                      # PropertyListing dataclass + legacy BrightData path
├── input-urls.json           # 2500 sample URLs for benchmarking
├── requirements.txt
└── README.md
```

## Why the BrightData approach was abandoned

`app/` still contains the old web_unlocker scraper for reference. It works but
costs ~$5,000/mo at this volume and BrightData blocks the GraphQL endpoint
that returns schools / price history / tax history — those fields only
populate for ~24% of listings via the HTML `__NEXT_DATA__` path. The
px_bypass path returns full data for 100% of listings.

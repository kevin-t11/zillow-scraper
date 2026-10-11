# Zillow PX-Bypass Scraper

Maintained repository: [kevin-t11/zillow-scraper](https://github.com/kevin-t11/zillow-scraper).
The original source remote is [ushixan/zillow-scraper](https://github.com/ushixan/zillow-scraper);
its Git history is retained. Runtime code in this snapshot matches the deployed
Lightsail service, including market search and both listing performance updates.
See [DEPLOYMENT.md](DEPLOYMENT.md) for configuration and verification.

High-throughput Zillow scraper that bypasses PerimeterX (HUMAN Bot Defender)
press-and-hold protection without paying for a captcha solver. Uses a patched
Chromium to mint wide-scope `_px3` cookies through an Evomi residential proxy,
then replays the cookies via concurrent `httpx` against Zillow's
`ForSaleNonPriorityQuery` GraphQL endpoint.

**Validated URL benchmark:** 2500 URLs in 119.8s — 100% success — 20.88 listings/sec.

**Local address benchmark (2026-10-08):** 1,000 distinct listings in 12.38s end to end, both caches disabled, 1,000/1,000 expected ZPIDs matched. Used 32 ready sessions at 10 requests per slot, fetch concurrency 300, and resolve concurrency 200. Inputs were prevalidated resolvable addresses; this is an observed run, not a latency guarantee.

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
  N cookies up front, route work to the least-loaded slot, and on 403 mark the slot dead and
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
EVOMI_PORT=1000            # default; HTTPS uses 1001
EVOMI_SCHEME=http          # HTTPS endpoints require https (inferred for port 1001)
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
python -m px_bypass.benchmark_urls --input input-urls.json --count 1000 --concurrency 240
python -m px_bypass.benchmark_addresses --input input.json --count 1000 \
  --concurrency 120 --resolve-concurrency 100
```

Run these against an already started API. Supply JSON arrays of input strings;
`input-urls.json` is user-provided and ignored by Git. Benchmark summaries are
written under the ignored `output/` directory. The address benchmark requires
the requested number of distinct successfully resolved properties.

## API

See [API.md](API.md) for the full reference, request limits, response fields,
caching behavior, errors, and client examples. Interactive docs are available
at `http://localhost:8000/docs` after the server starts.

| Endpoint | Input | Notes |
|---|---|---|
| `POST /v2/search/listings` | Market and filters | Discover listings and optionally fetch full details |
| `POST /v2/scrape/urls` | Zillow listing URLs | Direct scrape via cookie pool |
| `POST /v2/scrape/addresses` | Street addresses | Resolves via Zillow autocomplete while fetching completed resolutions |
| `GET /health` | — | Pool stats |

### `POST /v2/search/listings`

```bash
curl --compressed -X POST http://localhost:8000/v2/search/listings \
  -H 'Content-Type: application/json' \
  -d '{"location":"Naperville, IL","home_types":["multi_family"],"max_results":500,"include_details":true}'
```

Set `include_details` to `false` for search-card previews. Search results depend
on the upstream market response and the requested location and filters.

### `POST /v2/scrape/urls`

```bash
curl -X POST http://localhost:8000/v2/scrape/urls \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://www.zillow.com/homedetails/.../21091004_zpid/"],
    "concurrency": 240,
    "max_retries": 3
  }'
```

### `POST /v2/scrape/addresses`

```bash
curl -X POST http://localhost:8000/v2/scrape/addresses \
  -H "Content-Type: application/json" \
  -d '{
    "addresses": ["9315 Parrot Ave, Downey, CA 90240"],
    "concurrency": 120,
    "resolve_concurrency": 100
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

Health returns HTTP 200 when at least one validated listing session is available, otherwise HTTP 503. Evomi connectivity alone does not prove that Zillow permits listing retrieval. Sessions must pass a real property check before entering the pool.

## Tuning

| Variable | Default | Effect |
|---|---|---|
| `POOL_SIZE` | 24 | Cookies in rotation. Bigger = more throughput, more Evomi bandwidth on init. |
| `POOL_PER_SLOT_C` | 5 | Concurrent requests per cookie. Deployed VM uses 10; higher values can increase blocks and retries. |
| `concurrency` (request) | URLs: 240; addresses: 120 | Total in-flight requests. Effective ceiling = `POOL_SIZE × POOL_PER_SLOT_C`. |
| `resolve_concurrency` (address request) | 100 | Concurrent address lookups; overlaps listing fetches. |
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

The cloud deployment uses `POOL_SIZE=24`. Keep the cloud cookie pool at a
maximum of 32 sessions.

Use the deployed performance profile with:

```bash
python run.py --pool-size 24 --per-slot-c 10
```

The API defaults are listing concurrency 240 for URLs, listing concurrency 120
for addresses, and resolution concurrency 100. Use `cache_seconds: 0` for fresh
work, allow 120–180 seconds initially for a 1,000-input call, and avoid overlapping
large batches. See [API.md](API.md) for request examples and result validation.

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
│   ├── search.py             # market discovery and location filtering
│   ├── serve.py              # FastAPI app + lifespan-managed pool
│   ├── systemd-zillow.service
│   ├── benchmark_addresses.py # uncached address benchmark
│   └── benchmark_urls.py     # uncached URL benchmark
├── app/                      # PropertyListing dataclass + legacy BrightData path
├── input-urls.json           # user-provided benchmark input and ignored by Git
├── requirements.txt
└── README.md
```

## Why the BrightData approach was abandoned

`app/` still contains the old web_unlocker scraper for reference. It works but
costs ~$5,000/mo at this volume and BrightData blocks the GraphQL endpoint
that returns schools / price history / tax history — those fields only
populate for ~24% of listings via the HTML `__NEXT_DATA__` path. The
px_bypass path returns full data for 100% of listings.

To measure 1,000 distinct uncached addresses end to end, run `python -m px_bypass.benchmark_addresses --count 1000`. This checks successful unique ZPIDs and input ordering; see [API.md](API.md#uncached-performance-benchmark).

URL batches now preserve input order and fetch each distinct ZPID once. The shared server compresses large responses for clients supporting gzip. Run `python -m px_bypass.benchmark_urls --count 1000` to measure fully uncached URL fetches and verify every requested property ID.

The performance updates were deployed on 2026-10-08. Subsequent checks retrieved
1,000 distinct uncached addresses in 19.89s and 1,000 distinct uncached URL
listings in 34.33s, with all expected property IDs matched. These complete-response
times were measured on the VM and exclude external internet transfer and startup;
the URL run included session refreshes. Earlier preview measurements are retained
in [API.md](API.md#lightsail-comparison-2026-10-08). These are observed runs, not latency guarantees.

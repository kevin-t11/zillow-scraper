# Zillow Scraper API Reference

Reference for the v2 HTTP API started by `python run.py`. Discover listings by market, or send Zillow listing URLs or street addresses and receive property data with a batch summary. The runtime implementation in this repository matches the deployed Lightsail code as verified on 2026-10-15.

## Connection and interactive docs

Default local base URL: `http://localhost:8000`. Replace this with your deployed server URL when calling a remote instance.

| Resource | URL |
|---|---|
| Swagger UI with interactive requests | <http://localhost:8000/docs> |
| ReDoc reference | <http://localhost:8000/redoc> |
| OpenAPI JSON | <http://localhost:8000/openapi.json> |
| Service metadata and request schemas | <http://localhost:8000/info> |

Start the server after completing the setup in [README.md](README.md):

```bash
python run.py
```

The server validates an Evomi HTTP session before accepting requests and fills the rest of the pool in the background. The current default pool size is 24, with 5 concurrent requests per slot. Startup time varies. Browser-generated risk cookies are not imported by the default HTTP path.

Address and URL scraping use a single persisted property GET request upstream
for each unique uncached property. The response includes photos, description,
facts, and price/tax history when available from Zillow. Request parameters and
response fields for API callers are unchanged. Property requests use a pinned
Chrome-compatible HTTP transport through Evomi; discovery has a separate
HTTPX session pool. No additional headers or PX cookies are needed from API callers.

The application defines no API key or authentication header. A deployment may add authentication through its reverse proxy. POST requests use `Content-Type: application/json`. Clients advertising `Accept-Encoding: gzip` receive compressed responses larger than 1 KiB; clients requesting `identity` receive plain JSON. Compression preserves all result fields. Both scrape endpoints wait for completion and return JSON; they do not stream or return a job ID.

## Endpoint catalog

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Cookie pool status and counters |
| GET | `/info` | Service metadata, route catalog, and request schemas |
| POST | `/v2/search/listings` | Discover fresh listing previews by area and filters |
| POST | `/v2/scrape/urls` | Scrape listing URLs |
| POST | `/v2/scrape/addresses` | Resolve addresses and scrape their listings |

The legacy routes in `app/server.py`, such as `/scrape` and `/scrape/urls`, are not mounted by `run.py`.

## Discover listings by market

`POST /v2/search/listings`

This endpoint always returns fresh search-card previews. It does not fetch
property details or read/write the property cache.

Choose exactly one area mode:

- **Location + radius:** resolve a city, ZIP, county, or neighborhood and search
  within `radius_miles` of its resolved center (12 miles by default). The server
  uses a bounding rectangle upstream, then checks actual great-circle distance.
- **BBox:** send `north`, `south`, `east`, and `west` in decimal degrees. This
  skips location lookup and returns properties inside that rectangle, including
  its edges. Do not send `location` or `radius_miles` with `bbox`.

`strict_location: true` additionally intersects a location/radius search with
Zillow's selected region and checks city/state/ZIP text where available. It is
not a drawn polygon input. The default is now **false**, so a radius can include
neighboring towns. Existing callers that need the earlier region restriction
must explicitly send `strict_location: true`.

Both modes exclude relaxed/nearby suggestions and listings without usable
coordinates. Results are unique by ZPID. A rectangle is not a city boundary;
custom polygons and antimeridian-crossing boxes are not accepted.

| Field | Default | Limits or accepted values |
|---|---|---|
| `location` | None | 2–200 characters; required unless `bbox` is supplied |
| `radius_miles` | 12 | 0.25–100; location mode only |
| `bbox` | None | Object with `north`, `south`, `east`, `west`; south < north, west < east; latitude ±90, longitude ±180 |
| `strict_location` | false | Restrict location mode to its resolved Zillow region as well as the radius |
| `status` | `for_sale` | `for_sale`, `for_rent`, `sold` |
| `home_types` | `["multi_family"]` | Nonempty array: `single_family`, `multi_family`, `townhouse`, `condo`, `land`, `apartment`, `manufactured`, `apartment_or_condo`. Set this explicitly for the homes you want. |
| `new_construction_only` | false | For-sale searches only; excludes agent/owner resale, auction and foreclosure sources |
| `year_built_min`, `year_built_max` | None | Inclusive integer bounds, 1000–9999; either may be omitted; min ≤ max |
| `sold_in_last` | None | Sold searches only: `"any"`, `"1"`, `"7"`, `"14"`, `"30"`, `"90"`, `"6m"`, `"12m"`, `"24m"`, `"36m"`. Numeric strings mean days; `m` means months. |
| `max_results` | 500 | 1–2,000 unique matching properties; a cap, not a guaranteed count |
| `tile_if_needed` | true | Split only truncated areas; paginate dense leaf areas |
| `tile_depth` | 2 | 0–3 |
| `max_search_requests` | 25 | 1–100 search pages/tiles per request, excluding retries and session validation |
| `search_concurrency` | 4 | 1–16 simultaneous page/tile requests |
| `max_retries` | 3 | 1–10 attempts per upstream search request |
| `include_fsbo`, `include_new_construction`, `include_coming_soon` | true | For-sale inclusion flags; including new construction does not make it exclusive |
| `include_foreclosures`, `include_auctions` | false | For-sale inclusion flags |

`new_construction_only` overrides the other sale-source inclusion flags, but
cannot be combined with `include_new_construction: false`. Its coming-soon
listing-status setting still follows `include_coming_soon`.
Unknown fields, mixed area modes, inverted bounds/ranges, and incompatible
status/filter combinations return **HTTP 422** instead of silently ignoring inputs.

Location/radius — newly built homes for sale:

```bash
curl --compressed --max-time 180 \
  -X POST 'http://localhost:8000/v2/search/listings' \
  -H 'Content-Type: application/json' \
  -d '{
    "location": "Naperville, IL",
    "radius_miles": 10,
    "home_types": ["single_family", "townhouse"],
    "new_construction_only": true,
    "year_built_min": 2020,
    "year_built_max": 2026,
    "max_results": 100
  }'
```

BBox — homes sold in the last 12 months:

```bash
curl --compressed --max-time 180 \
  -X POST 'http://localhost:8000/v2/search/listings' \
  -H 'Content-Type: application/json' \
  -d '{
    "bbox": {"north": 41.84, "south": 41.66, "east": -88.05, "west": -88.25},
    "status": "sold",
    "sold_in_last": "12m",
    "home_types": ["single_family"],
    "year_built_min": 2000,
    "max_results": 100
  }'
```

Parallel search responses are processed as they arrive. Once enough unique
in-area listings are found, unfinished page/tile requests are cancelled.
Results have no guaranteed order. The search request count includes requests
started and then cancelled after reaching the result cap.

Both modes return `results` and `summary`. `summary.area` echoes the actual
bbox, radius center, radius and region restriction. `summary.applied_filters`
reports the effective filters. `summary.search` includes upstream count,
request count, discarded geography/missing-coordinate counts, `limit_reached`
and `coverage_limited`. The upstream count can include unmappable cards or
properties outside the radius circle; it is not the returned unique count.
`coverage_limited: true` means a request budget, upstream cap, unknown count,
or repeated page prevented complete enumeration. `limit_reached: true` means
the requested result cap was met. Neither promises that every property in a
large area has been enumerated.

Results include the Zillow URL/ZPID, address, price, beds/baths, area, home
and listing type, coordinates, preview image, and any description available on
the search card. Fields can be empty or null when Zillow omits them. There is
no detail mode, full photo gallery, or per-property detail request.

**Client migration:** omit `include_details`, `concurrency`, and `cache_seconds`
from listings requests. These removed fields now return HTTP 422 (including
`include_details: false`). Use `search_concurrency` to control discovery work.
Address and URL scrape endpoints retain their own detail and cache behavior.

Discovery uses its own validated Evomi session pool (`SEARCH_POOL_SIZE`, default
4), independently of property-detail sessions. `/health` reports both pools and
returns HTTP 503 with `status: "search-only"` when only discovery is available;
the listings endpoint still works in that state. No live search sessions
produces HTTP 503; upstream search/location errors produce HTTP 502.

## Scrape by URL

`POST /v2/scrape/urls`

| Field | Type | Required | Default | Limits |
|---|---|---|---|---|
| `urls` | string array | Yes | — | 1–10,000 entries |
| `concurrency` | integer | No | 240 | 1–300 |
| `max_retries` | integer | No | 3 | 1–10 |
| `cache_seconds` | integer | No | 86400 | 0–86400; 0 bypasses cache reads and writes |

Use listing URLs containing a numeric ZPID, for example `/homedetails/9315-Parrot-Ave-Downey-CA-90240/21091004_zpid/`. The schema validates strings and array length, not URL syntax; an invalid listing URL can produce a per-result `error` in an HTTP 200 response.

```bash
curl --request POST 'http://localhost:8000/v2/scrape/urls' \
  --header 'Content-Type: application/json' \
  --data '{
    "urls": ["https://www.zillow.com/homedetails/9315-Parrot-Ave-Downey-CA-90240/21091004_zpid/"],
    "concurrency": 240,
    "max_retries": 3,
    "cache_seconds": 86400
  }'
```

**Ordering:** URL results preserve request order, including cached hits, duplicate inputs, URL aliases, and failures. URLs for the same valid ZPID share one fetch within the batch; each submitted URL retains a separate result row. `cache_seconds: 0` disables cross-request caches while retaining this within-batch deduplication.

## Scrape by address

`POST /v2/scrape/addresses`

| Field | Type | Required | Default | Limits |
|---|---|---|---|---|
| `addresses` | string array | Yes | — | 1–10,000 entries |
| `concurrency` | integer | No | 120 | 1–300; listing fetches |
| `resolve_concurrency` | integer | No | 100 | 1–200; address resolution |
| `max_retries` | integer | No | 3 | 1–10 |
| `use_search_fallback` | boolean | No | true | Enables search fallback during resolution |
| `cache_seconds` | integer | No | 86400 | 0–86400; applies to resolution and listing caches |

Include street, city, state, and ZIP code for precise resolution. Each address resolves to a Zillow listing URL, then its property fetch starts while other addresses are still resolving. Autocomplete tries the complete address first and retains broader query fallbacks. Explicit apartment, unit, space, and lot IDs must match the suggestion; those addresses use up to 12 suggestions and skip the unverified first-result search fallback.

```bash
curl --request POST 'http://localhost:8000/v2/scrape/addresses' \
  --header 'Content-Type: application/json' \
  --data '{
    "addresses": ["9315 Parrot Ave, Downey, CA 90240"],
    "concurrency": 120,
    "resolve_concurrency": 100,
    "max_retries": 3,
    "use_search_fallback": true,
    "cache_seconds": 86400
  }'
```

Results are stitched back into address input order, with `input_address` set to the submitted address. An unresolved address returns `status: "not_found"` and `error_msg: "Address could not be resolved to a ZPID"`.

Duplicate addresses share resolution work within the batch, and addresses resolving to the same ZPID share a listing fetch. Every input retains its own result row and `input_address`, in input order. Failed fetches retain a result row too. `cache_seconds: 0` disables cross-request caches; it still avoids duplicate work within a single request.

## Response structure

Both scrape endpoints return:

```json
{
  "results": [],
  "summary": {}
}
```

Each result contains all fields from the property model except `raw_json`. A successful scrape can still have missing property fields: strings generally default to `""`, optional numbers and booleans to `null`, and lists to `[]`. Inspect `status` for every result, even after HTTP 200.

Illustrative result excerpt; actual results include the other fields listed below:

```json
{
  "input_address": "9315 Parrot Ave, Downey, CA 90240",
  "status": "success",
  "error_msg": "",
  "attempts": 1,
  "zpid": "21091004",
  "full_address": "9315 Parrot Ave, Downey, CA 90240",
  "price": 1150000,
  "price_str": "$1,150,000",
  "bedrooms": 4,
  "bathrooms": 3,
  "living_area_sqft": 2169,
  "schools": [],
  "price_history": [],
  "tax_history": [],
  "nearby_homes": []
}
```

### Result status values

| Status | Meaning |
|---|---|
| `success` | Property data parsed successfully |
| `blocked` | Scraper classified the failed fetch as blocked, typically an upstream 403 |
| `not_found` | Address resolution failed, or scraper classified the fetch as an upstream 404 |
| `error` | Invalid listing URL, network failure, or another fetch error |
| `parse_error` | Upstream response could not be parsed into a property |

`partial` exists in the shared model's comments but is not emitted by the current v2 implementation. `listing_status` is a separate upstream property status, such as `FOR_SALE` or `SOLD`.

### Property fields

Types below follow the internal model. Responses use untyped dictionaries, so upstream values are not strictly validated against these annotations.

| Fields | Type | Meaning |
|---|---|---|
| `input_address`, `status`, `error_msg` | string | Input identifier, scrape outcome, error details |
| `attempts` | integer | Recorded attempt count; 0 when no attempt was recorded |
| `zpid` | string | Zillow property ID |
| `full_address`, `street`, `city`, `state`, `zipcode`, `url` | string | Property address and listing URL |
| `price`, `zestimate`, `rent_zestimate` | number or null | Listing price and Zillow estimates |
| `price_str` | string | Formatted price |
| `bedrooms`, `bathrooms` | number or null | Bed and bath counts |
| `living_area_sqft`, `lot_size_sqft` | number or null | Area values passed through from Zillow; verify upstream units where relevant |
| `home_type` | string | Upstream home type |
| `year_built`, `parking_spaces`, `garage_spaces` | integer or null | Construction year and parking capacities |
| `has_cooling`, `has_heating` | boolean or null | HVAC availability |
| `heating_type`, `cooling_type` | string | HVAC descriptions |
| `listing_status` | string | Upstream listing status |
| `days_on_zillow`, `views`, `saves` | integer or null | Listing activity |
| `listing_date`, `sold_date` | string | Upstream date strings; may be empty |
| `agent_name`, `agent_phone`, `agent_email` | string | Listing agent details |
| `broker_name`, `listing_office`, `mls_id` | string | Brokerage and MLS details |
| `last_sold_price` | number or null | Last sold price |
| `last_sold_date` | string | Sold date derived from price history |
| `monthly_hoa_fee`, `property_tax_rate`, `price_per_sqft`, `annual_tax` | number or null | Upstream fee, rate, and tax values; rates are not rescaled |
| `tax_year` | integer or null | Currently always null in v2 |
| `mortgage_rate_30yr`, `mortgage_rate_15yr`, `mortgage_rate_arm5` | number or null | Currently always null in v2 |
| `description` | string | Property description |
| `highlights`, `appliances`, `interior_features`, `exterior_features`, `community_features`, `parking_features` | array | Upstream highlights and features |
| `latitude`, `longitude` | number or null | Coordinates |
| `neighborhood`, `county` | string | Location details |
| `photo_count` | integer | Upstream photo count or available photo count |
| `photos` | string array | Up to 50 photo URLs; count may exceed returned array length |
| `virtual_tour_url` | string | External tour URL |
| `schools`, `price_history`, `tax_history`, `nearby_homes` | object array | Related records described below |

### Related record fields

| Array | Fields per record |
|---|---|
| `schools` | `name`, `rating`, `grades`, `distance`, `level`, `link` |
| `price_history` | `date`, `price`, `event`, `source`, `price_per_sqft`, `price_change_rate`, `source_data` |
| `tax_history` | `date`, `tax_paid`, `tax_increase_rate`, `value`, `value_increase_rate`, `source_data` |
| `nearby_homes` | `zpid`, `price`, `price_str`, `bedrooms`, `bathrooms`, `living_area`, `address`, `street`, `city`, `state`, `zipcode`, `url`, `image`, `status`, `latitude`, `longitude`, `source_data` |

Tax history dates are derived from Unix milliseconds and formatted as `YYYY-MM-DD`. `source_data` retains the upstream related record. Nearby home URLs can be relative paths. Nearby homes use `living_area`, rather than the parent result's `living_area_sqft`.

### Batch summary

| Shared field | Meaning |
|---|---|
| `total` | Number of submitted inputs |
| `success`, `blocked`, `not_found`, `other_error` | Outcome counters; `other_error` includes errors outside blocked/not found |
| `elapsed_s` | Total route processing time in seconds |
| `scrape_elapsed_s` | Fresh scraping time, or 0 when no fetch was needed |
| `rate_per_sec` | Fresh scrape batch size divided by scrape time; 0 when no fetch was needed |
| `cache` | Process-level cache sizes and cumulative hit/miss counters |
| `pool` | Cookie pool stats; can be `{}` if unavailable |

URL summaries also include `cached` (input rows), `fetched` (distinct fresh fetches), `unique_total`, `deduplicated`, and `bytes_total` (the scraper's recorded response-byte total). Their cache object contains `listings`, `hits`, and `misses`.

Address summaries also include `resolve_cached`, `resolve_fetched` (successful fresh resolutions), `unresolved`, `listing_cached`, `listing_fetched`, and `resolve_elapsed_s`. Their cache object adds `resolutions`; hits and misses combine both caches. Address summaries do not include `bytes_total`. `resolve_cached`, `resolve_fetched`, `unresolved`, and outcome counters count input rows. `listing_cached` and `listing_fetched` count distinct listings. Resolution and scraping overlap: their durations must not be added. For the pipelined address path, `scrape_elapsed_s` includes time waiting for further resolved URLs, and `rate_per_sec` measures the fresh fetch pipeline throughput.

## Health and service information

```bash
curl 'http://localhost:8000/health'
curl 'http://localhost:8000/info'
```

Illustrative health response:

```json
{
  "status": "ok",
  "size": 24,
  "live": 24,
  "mints": 24,
  "refreshes": 0,
  "rehydrated": 0
}
```

`size` is configured pool capacity, `live` is usable slots, `mints` counts successful fresh mints, `refreshes` counts refreshes, and `rehydrated` counts restored slots. Actual counters depend on startup and usage. Health returns HTTP 200 only when both property-detail and search pools have validated sessions. `search_pool` contains discovery-session counters. A partial outage returns HTTP 503 with `status: "search-only"` or `"details-only"`; when neither pool is available it reports `"no-cookies"`. `refreshing`, `validation_failures`, and `last_error` describe recovery. Uncached scrape requests return HTTP 503 with `Retry-After: 60` while unavailable. If all sessions fail during a batch, session acquisition waits at most 30 seconds before remaining rows receive `status: "blocked"`. Recovery retries pause for 60 seconds after a failure. Sessions expire after 25 minutes by default during runtime and are validated again when restored at startup. `session_strategy` identifies the HTTP or legacy browser path. `recovery_active`, `last_recovery_started_at`, and `last_recovery_completed_at` expose recovery progress; `oldest_session_age_s` and `last_success_at` describe usable sessions. Validation is a point-in-time check, not a guarantee that the next request will succeed. Every actual network attempt, including 403 and invalid JSON responses, consumes `max_retries`.

`/info` returns `name`, `version`, `description`, and an `endpoints` array with request schemas and examples.

## Errors

| HTTP status | Meaning | Client action |
|---|---|---|
| 200 | Batch processed, including possible per-result failures | Inspect each result's `status` and `error_msg` |
| 422 | Request validation failed | Fix missing fields, list lengths, types, or numeric limits using `detail` |
| 503 | A fresh fetch needs a pool that is unavailable or has no live slots | Retry after pool readiness recovers |
| 500 | Unhandled server error | Check server logs before retrying |

The pool readiness guard returns one of these error bodies:

```json
{"detail": "Server is still booting — pool not ready."}
```

```json
{"detail": "Cookie pool empty — all slots refreshing."}
```

Normal startup finishes before requests are accepted. Fully cached requests can succeed without a live pool because readiness is checked only when a fresh listing fetch is needed.

## Caching and client usage

Listing cache keys are ZPIDs. Address cache keys lowercase addresses and collapse whitespace; they do not normalize punctuation or abbreviations. Both caches are in memory per process, limited to 50,000 entries each, and cleared by a process restart. Cached entries retain the TTL set when written; a later request's `cache_seconds` does not impose a new maximum age on existing entries. Use `cache_seconds: 0` to force fresh work without deleting existing cached entries.

For large batches, set a client timeout that allows resolution, retries, and fresh fetching to finish. The application defines no request-level timeout or background-job polling endpoint. Split batches if your deployment's proxy or client timeout requires it.

Python example using the project's `httpx` dependency:

```python
import httpx

with httpx.Client(base_url="http://localhost:8000", timeout=300.0) as client:
    response = client.post(
        "/v2/scrape/addresses",
        json={"addresses": ["9315 Parrot Ave, Downey, CA 90240"]},
    )
    response.raise_for_status()
    payload = response.json()
    for result in payload["results"]:
        if result["status"] == "success":
            print(result["zpid"], result["full_address"], result["price"])
        else:
            print(result["input_address"], result["status"], result["error_msg"])
    print(payload["summary"])
```

The 300-second client timeout is an example, not a server guarantee.

## Uncached performance benchmark

With the server already running, benchmark distinct addresses with both cache reads and writes disabled:

```bash
python -m px_bypass.benchmark_addresses --count 1000 \
  --concurrency 240 --resolve-concurrency 100
```

Listing workers pick the least-loaded live cookie session and retain the per-session concurrency cap, so a slow session does not accumulate work while others are available.

The benchmark includes HTTP response transfer and JSON parsing in `client_elapsed_s`. It verifies input ordering and counts distinct successful ZPIDs so duplicate listings or failed resolutions cannot satisfy the 1,000-listing target. Optionally pass `--expected-zpids <address-to-zpid.json>` to verify listing identities against known IDs. Reports are saved under `output/`. Server startup is measured separately.

Effective listing concurrency is limited by live pool slots multiplied by `POOL_PER_SLOT_C`; setting request concurrency above that capacity does not raise the pool limit. For example, 24 live slots at 10 requests per slot support 240 concurrent listing requests. Higher per-slot concurrency can increase blocks and retries. A 10–20 second target requires 50–100 successful distinct listings per second across resolution, fetching, and response delivery; the pipeline change alone does not establish that performance.

### Validated local result and settings

On 2026-10-08, a server-ready local HTTP run retrieved 1,000 distinct uncached listings in **12.38 seconds**, including response transfer and JSON parsing. All 1,000 returned ZPIDs matched expected property IDs; no results failed or were blocked. Both cache sizes and cache-hit counts remained zero. Inputs were prevalidated resolvable addresses. This measures one local run, not a percentile or deployment guarantee; arbitrary addresses can fail resolution, and upstream retries can extend latency.

Server settings:

```bash
python run.py --pool-size 32 --per-slot-c 10
```

Request settings (alongside your `addresses` array):

```json
{
  "concurrency": 300,
  "resolve_concurrency": 200,
  "cache_seconds": 0
}
```

Reproduce with the local validated input and identity map:

```bash
python -m px_bypass.benchmark_addresses --count 1000 \
  --input output/unique-address-benchmark.input.json \
  --expected-zpids output/unique-address-benchmark.expected.json \
  --concurrency 300 --resolve-concurrency 200
```

The inputs and report are local benchmark artifacts in the ignored `output/` directory. The report is `output/addresses-pipeline-1000-20261008T030153Z.summary.json`.

## URL performance benchmark

Use `python -m px_bypass.benchmark_urls --count 1000 --concurrency 240` with a ready server. The tool selects distinct ZPIDs, sets `cache_seconds: 0`, verifies that every requested property succeeded without a cache hit, and records complete response transfer and JSON parsing in `client_elapsed_s`. It also records compressed `wire_content_length`, decoded `response_bytes`, and `content_encoding`.

A local run on 2026-10-08 retrieved 1,000 distinct uncached URL listings in **13.13 seconds**, with all IDs matched. It used 32 ready sessions, 10 requests per slot, and request concurrency 300. Gzip reduced the 33,098,669-byte decoded body to 5,597,077 bytes on the connection. The report is `output/urls-1000-c300-20261008T032357Z.summary.json`. This is a local observation; deployment and network conditions can differ.

### Lightsail comparison (2026-10-08)

Every run below fetched the same 1,000 distinct URLs with `cache_seconds: 0`; all 1,000 requested property IDs succeeded. VM runs were measured through an SSH tunnel from this computer, including response transfer and JSON parsing. Server startup is excluded. Optimized runs used an isolated temporary server on the same VM, leaving the live service and its search endpoint intact. Its authentication sessions were copied from the live pool; property responses were fetched fresh.

| Configuration | VM route processing | Client end to end |
|---|---:|---:|
| Existing live code, 24 sessions, concurrency 60 | 62.44s | 85.26s |
| Existing live code, 24 sessions, concurrency 240 | 65.44s | 74.00s |
| Optimized preview, 24 sessions, concurrency 240 | 19.88s | 24.77s |
| Optimized preview, 32 sessions, concurrency 300 | 32.03s | 36.73s |

The 32-session run required session refreshes. The recommended observed VM configuration is **24 sessions × 10 requests per session**, with request concurrency **240**. The 24-session optimized run compressed a 33,098,669-byte JSON body to 5,597,557 bytes. Merely increasing concurrency did not improve VM fetch processing in these trials. Performance depends on session health and network conditions; these are individual measurements, not a guarantee.

The optimized code was subsequently deployed on 2026-10-08 with the existing search endpoint preserved. Post-deployment tests returned all 1,000 expected distinct properties fresh through each scrape endpoint: addresses took 19.89s and URLs took 34.33s, including response receipt and parsing on the VM. External transfer and startup are excluded; the URL run included session refreshes. See [DEPLOYMENT.md](DEPLOYMENT.md) for the deployed profile and verification. Earlier comparison data remains in the ignored local `output/` directory.


### HTTPS proxy connection optimization (2026-10-11)

The session pool reuses one verified TLS context for its HTTPS proxy connections.
Previously, every new proxy connection loaded the system and certifi certificate
stores synchronously on the event loop. A 300-listing VM profile attributed
14.33 seconds to these repeated certificate-store loads. The shared context
retains hostname and certificate verification; each sticky session still has
its own HTTP client, connections, and cookie jar.

No request changes are needed. Use the URL endpoint when you already have
listing URLs, retain its default `concurrency: 240`, send `cache_seconds: 0`
for fresh retrieval, and accept gzip responses. URL requests skip address
resolution. Both endpoints benefit from the connection fix.

Live verification after deployment:

| Endpoint/run | Distinct successful listings | Route processing | Public client total |
|---|---:|---:|---:|
| Urls (1) | 1,000 / 1,000 | 13.47 s | 18.90 s |
| Addresses (2) | 1,000 / 1,000 | 14.47 s | 20.71 s |
| Urls (3) | 1,000 / 1,000 | 13.06 s | 20.42 s |

All runs used `cache_seconds: 0`, matched every expected property ID, and
completed each listing in one attempt with zero blocks. The same 1,000 unique
properties were used across endpoints; both caches stayed empty. All 24
sessions remained healthy. Public totals include complete response delivery
and JSON parsing, and exclude startup. Responses retained all existing fields,
including photos and price/tax histories. These are measured runs, not a latency
guarantee; the repeat URL call took 20.42 seconds including internet transfer.

Before this fix, the same URL test at concurrency 240 took 28.78 seconds in
the route and 33.79 seconds end to end. Reducing concurrency alone to 120 took
19.71 seconds in the route and 23.95 seconds end to end. The connection fix
keeps the URL default at 240 and reduces the measured route time to about
13 seconds. Address resolution already overlaps with listing retrieval, so
skipping it does not remove its full duration from total latency.

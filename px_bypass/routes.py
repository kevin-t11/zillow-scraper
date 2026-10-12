"""HTTP route handlers — kept separate from server bootstrap for clarity."""

from __future__ import annotations

import re
import time
from contextlib import aclosing
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import PropertyListing
from .cache import TTLCache, normalize_address
from .resolver import iter_resolved_addresses
from .scraper import parse_listing_url, scrape_urls_via_px
from .search import SearchError, discover_zillow_listings

if TYPE_CHECKING:
    from .cookie_pool import CookiePool

router = APIRouter()

ZPID_RE = re.compile(r"/(\d+)_zpid")

# Module-level caches — shared across requests. Per-process, in-memory.
_listing_cache: TTLCache[PropertyListing] = TTLCache()
_resolve_cache: TTLCache[str] = TTLCache()


# ─────────────── Pydantic schemas ───────────────


class ScrapeUrlsRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=10000)
    concurrency: int = Field(240, ge=1, le=300)
    max_retries: int = Field(3, ge=1, le=10)
    cache_seconds: int = Field(86400, ge=0, le=86400)  # 24 hour default; 0 disables


class ScrapeAddressesRequest(BaseModel):
    addresses: list[str] = Field(..., min_length=1, max_length=10000)
    concurrency: int = Field(120, ge=1, le=300)
    resolve_concurrency: int = Field(100, ge=1, le=200)
    max_retries: int = Field(3, ge=1, le=10)
    use_search_fallback: bool = True
    cache_seconds: int = Field(86400, ge=0, le=86400)


ZillowSearchStatus = Literal["for_sale", "for_rent", "sold"]
ZillowHomeType = Literal[
    "single_family",
    "multi_family",
    "townhouse",
    "condo",
    "land",
    "apartment",
    "manufactured",
    "apartment_or_condo",
]


class SearchBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    north: float = Field(..., ge=-90, le=90, strict=True)
    south: float = Field(..., ge=-90, le=90, strict=True)
    east: float = Field(..., ge=-180, le=180, strict=True)
    west: float = Field(..., ge=-180, le=180, strict=True)

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.south >= self.north or self.west >= self.east:
            raise ValueError("bbox requires south < north and west < east; split antimeridian searches into two boxes")
        return self


class SearchListingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    location: str | None = Field(None, min_length=2, max_length=200)
    bbox: SearchBounds | None = None
    status: ZillowSearchStatus = "for_sale"
    home_types: list[ZillowHomeType] = Field(default_factory=lambda: ["multi_family"], min_length=1)
    max_results: int = Field(500, ge=1, le=2000)
    radius_miles: float = Field(12.0, ge=0.25, le=100, strict=True)
    tile_if_needed: bool = True
    tile_depth: int = Field(2, ge=0, le=3)
    search_concurrency: int = Field(4, ge=1, le=16)
    max_search_requests: int = Field(25, ge=1, le=100)
    max_retries: int = Field(3, ge=1, le=10)
    include_fsbo: bool = True
    include_new_construction: bool = True
    include_coming_soon: bool = True
    include_foreclosures: bool = False
    include_auctions: bool = False
    strict_location: bool = Field(False, description="Also restrict location/radius searches to Zillow's resolved region.")
    new_construction_only: bool = False
    year_built_min: int | None = Field(None, ge=1000, le=9999, strict=True)
    year_built_max: int | None = Field(None, ge=1000, le=9999, strict=True)
    sold_in_last: Literal["any", "1", "7", "14", "30", "90", "6m", "12m", "24m", "36m"] | None = None

    @model_validator(mode="after")
    def validate_search(self):
        if (self.location is None) == (self.bbox is None):
            raise ValueError("Provide either location (with optional radius_miles) or bbox, exclusively")
        if self.bbox is not None:
            if "radius_miles" in self.model_fields_set:
                raise ValueError("radius_miles cannot be combined with bbox")
            if self.strict_location:
                raise ValueError("strict_location requires location, not bbox")
        if self.year_built_min is not None and self.year_built_max is not None and self.year_built_min > self.year_built_max:
            raise ValueError("year_built_min must be <= year_built_max")
        if self.sold_in_last is not None and self.status != "sold":
            raise ValueError("sold_in_last requires status='sold'")
        if self.new_construction_only:
            if self.status != "for_sale":
                raise ValueError("new_construction_only requires status='for_sale'")
            if not self.include_new_construction:
                raise ValueError("new_construction_only conflicts with include_new_construction=false")
        return self


class ScrapeResponse(BaseModel):
    results: list[dict]
    summary: dict


# ─────────────── helpers ───────────────


def _pool(request: Request) -> "CookiePool":
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "Server is still booting — pool not ready.")
    if pool.stats["live"] == 0:
        raise HTTPException(503, "No validated scraping session is available; recovery is in progress.",
                            headers={"Retry-After": "60"})
    return pool


def _search_pool(request: Request) -> "CookiePool":
    pool = getattr(request.app.state, "search_pool", None)
    if pool is None:
        raise HTTPException(503, "Search sessions are not ready", headers={"Retry-After": "60"})
    if pool.stats["live"] == 0:
        raise HTTPException(503, "No validated search session is available; recovery is in progress",
                            headers={"Retry-After": "60"})
    return pool


# ─────────────── routes ───────────────


@router.get("/info")
async def info() -> dict:
    """Describe every endpoint exposed by this service."""
    return {
        "name": "Zillow PX Bypass",
        "version": "2.0",
        "description": (
            "Fetches public Zillow listing details through "
            "validated Evomi HTTP sessions with bounded retries."
        ),
        "endpoints": [
            {
                "method": "GET",
                "path": "/health",
                "summary": "Pool liveness + stats.",
            },
            {
                "method": "GET",
                "path": "/info",
                "summary": "This endpoint — service metadata and route catalog.",
            },
            {
                "method": "POST",
                "path": "/v2/scrape/urls",
                "summary": "Scrape Zillow listing URLs directly.",
                "request": ScrapeUrlsRequest.model_json_schema(),
                "example": {
                    "urls": [
                        "https://www.zillow.com/homedetails/9315-Parrot-Ave-Downey-CA-90240/21091004_zpid/"
                    ],
                    "concurrency": 240,
                    "max_retries": 3,
                    "cache_seconds": 86400,
                },
            },
            {
                "method": "POST",
                "path": "/v2/scrape/addresses",
                "summary": (
                    "Resolve street addresses and fetch listings concurrently."
                ),
                "request": ScrapeAddressesRequest.model_json_schema(),
                "example": {
                    "addresses": ["9315 Parrot Ave, Downey, CA 90240"],
                    "concurrency": 120,
                    "resolve_concurrency": 100,
                    "use_search_fallback": True,
                    "cache_seconds": 86400,
                },
            },
            {
                "method": "POST",
                "path": "/v2/search/listings",
                "summary": (
                    "Discover listings inside a location/radius or bbox, with construction, "
                    "year-built and sold-date filters. Returns fresh search-card previews."
                ),
                "request": SearchListingsRequest.model_json_schema(),
                "example": {
                    "location": "Naperville, IL",
                    "status": "for_sale",
                    "home_types": ["multi_family"],
                    "max_results": 500,
                    "radius_miles": 12,
                },
                "bbox_example": {
                    "bbox": {"north": 41.84, "south": 41.66, "east": -88.05, "west": -88.25},
                    "status": "sold", "sold_in_last": "12m",
                    "home_types": ["single_family"], "year_built_min": 2000,
                },
            },
            {
                "method": "GET",
                "path": "/docs",
                "summary": "Swagger UI (auto-generated from schemas).",
            },
        ],
    }


@router.get("/health")
async def health(request: Request):
    pool = request.app.state.pool
    payload = {
        "status": "ok" if pool.stats["live"] > 0 else "no-cookies",
        **pool.stats,
    }
    search_pool = getattr(request.app.state, "search_pool", None)
    if search_pool is not None:
        payload["search_pool"] = search_pool.stats
        if pool.stats["live"] == 0 and search_pool.stats["live"] > 0:
            payload["status"] = "search-only"
        elif pool.stats["live"] > 0 and search_pool.stats["live"] == 0:
            payload["status"] = "details-only"
    healthy = pool.stats["live"] > 0 and (search_pool is None or search_pool.stats["live"] > 0)
    return JSONResponse(payload, status_code=200 if healthy else 503)


def _zpid_of(url: str) -> str | None:
    m = ZPID_RE.search(url)
    return m.group(1) if m else None


@router.post("/v2/scrape/urls", response_model=ScrapeResponse)
async def scrape_urls_v2(req: ScrapeUrlsRequest, request: Request) -> ScrapeResponse:
    t_total = time.monotonic()

    def key_of(url: str) -> str:
        try:
            return "zpid:" + parse_listing_url(url)[1]
        except ValueError:
            return "url:" + url

    keys = [key_of(url) for url in req.urls]
    cached: dict[str, PropertyListing] = {}
    misses: dict[str, str] = {}
    for key, url in zip(keys, req.urls):
        if key in cached or key in misses:
            continue
        zpid = key.removeprefix("zpid:") if key.startswith("zpid:") else None
        hit = _listing_cache.get(zpid) if zpid and req.cache_seconds > 0 else None
        if hit is not None:
            cached[key] = hit
        else:
            misses[key] = url

    stats = None
    fresh: list[PropertyListing] = []
    if misses:
        pool = _pool(request)
        fresh, stats = await scrape_urls_via_px(
            misses.values(), concurrency=req.concurrency, max_retries=req.max_retries,
            skip_direct=True, pool=pool,
        )
        for listing in fresh:
            if listing.status == "success" and listing.zpid:
                _listing_cache.set(listing.zpid, listing, req.cache_seconds)

    by_key = dict(cached)
    for listing in fresh:
        key = ("zpid:" + listing.zpid if listing.zpid
               else key_of(listing.url or listing.input_address))
        by_key[key] = listing

    results: list[PropertyListing] = []
    for key, url in zip(keys, req.urls):
        listing = by_key.get(key)
        if listing is None:
            listing = PropertyListing(status="error", error_msg="No fetched result matched the URL")
        results.append(replace(listing, input_address=url, url=url))

    return ScrapeResponse(
        results=[r.to_dict() for r in results],
        summary={
            "total": len(req.urls),
            "unique_total": len(set(keys)),
            "deduplicated": len(req.urls) - len(set(keys)),
            "cached": sum(key in cached for key in keys),
            "fetched": len(fresh),
            "success": sum(r.status == "success" for r in results),
            "blocked": sum(r.status == "blocked" for r in results),
            "not_found": sum(r.status == "not_found" for r in results),
            "other_error": sum(r.status not in {"success", "blocked", "not_found"}
                               for r in results),
            "elapsed_s": round(time.monotonic() - t_total, 2),
            "scrape_elapsed_s": round(stats.elapsed_s, 2) if stats else 0.0,
            "rate_per_sec": round(stats.rate, 2) if stats else 0.0,
            "bytes_total": stats.bytes_total if stats else 0,
            "cache": {
                "listings": _listing_cache.size,
                "hits": _listing_cache.hits,
                "misses": _listing_cache.misses,
            },
            "pool": _pool_stats_or_empty(request),
        },
    )


def _pool_stats_or_empty(request: Request) -> dict:
    try:
        return request.app.state.pool.stats
    except Exception:
        return {}


@router.post("/v2/scrape/addresses", response_model=ScrapeResponse)
async def scrape_addresses_v2(
    req: ScrapeAddressesRequest, request: Request,
) -> ScrapeResponse:
    t_total = time.monotonic()

    # Resolution feeds the fetch workers immediately; neither stage waits for
    # the entire batch. Keep indices separately so aliases preserve input order.
    resolved_urls: list[str | None] = [None] * len(req.addresses)
    to_resolve: list[str] = []
    pending_indices: dict[str, list[int]] = {}
    resolve_cached = 0
    for index, addr in enumerate(req.addresses):
        key = normalize_address(addr)
        url = _resolve_cache.get(key) if req.cache_seconds > 0 else None
        if url:
            resolved_urls[index] = url
            resolve_cached += 1
        else:
            if key not in pending_indices:
                pending_indices[key] = []
                to_resolve.append(addr)
            pending_indices[key].append(index)

    cached: dict[str, PropertyListing] = {}
    queued: set[str] = set()
    resolve_fetched = 0
    resolve_elapsed = 0.0
    t_resolve = time.monotonic()

    def listing_key(url: str) -> str:
        return _zpid_of(url) or url

    def needs_fetch(url: str) -> bool:
        key = listing_key(url)
        if key in queued or key in cached:
            return False
        hit = _listing_cache.get(key) if req.cache_seconds > 0 else None
        if hit is not None:
            cached[key] = hit
            return False
        queued.add(key)
        return True

    async def pending_urls():
        nonlocal resolve_fetched, resolve_elapsed
        for url in resolved_urls:
            if url is not None and needs_fetch(url):
                yield url
        async with aclosing(iter_resolved_addresses(
            to_resolve, concurrency=req.resolve_concurrency,
            use_search_fallback=req.use_search_fallback,
        )) as resolutions:
            async for index, resolved in resolutions:
                addr = to_resolve[index]
                key = normalize_address(addr)
                if resolved is None:
                    continue
                url = resolved[2]
                for input_index in pending_indices[key]:
                    resolved_urls[input_index] = url
                resolve_fetched += len(pending_indices[key])
                _resolve_cache.set(key, url, req.cache_seconds)
                if needs_fetch(url):
                    yield url
        resolve_elapsed = time.monotonic() - t_resolve

    stats = None
    fresh: list[PropertyListing] = []
    async with aclosing(pending_urls()) as urls:
        # Peeking allows fully cached/unresolved batches to skip pool readiness.
        first_url = await anext(urls, None)
        if first_url is not None:
            pool = _pool(request)

            async def fetch_urls():
                yield first_url
                async for url in urls:
                    yield url

            fresh, stats = await scrape_urls_via_px(
                fetch_urls(), concurrency=req.concurrency,
                max_retries=req.max_retries, skip_direct=True, pool=pool,
            )

    by_key = dict(cached)
    for listing in fresh:
        key = listing.zpid or listing_key(listing.url or listing.input_address)
        by_key[key] = listing
        if listing.status == "success" and listing.zpid:
            _listing_cache.set(listing.zpid, listing, req.cache_seconds)

    listings: list[PropertyListing] = []
    for addr, url in zip(req.addresses, resolved_urls):
        if url is None:
            listing = PropertyListing(
                input_address=addr, status="not_found",
                error_msg="Address could not be resolved to a ZPID",
            )
        else:
            listing = by_key.get(listing_key(url))
            if listing is None:
                listing = PropertyListing(
                    url=url, status="error",
                    error_msg="No fetched result matched the resolved listing",
                )
            listing = replace(listing, input_address=addr)
        listings.append(listing)

    return ScrapeResponse(
        results=[r.to_dict() for r in listings],
        summary={
            "total": len(req.addresses),
            "resolve_cached": resolve_cached,
            "resolve_fetched": resolve_fetched,
            "unresolved": sum(url is None for url in resolved_urls),
            "listing_cached": len(cached),
            "listing_fetched": len(fresh),
            "success": sum(r.status == "success" for r in listings),
            "blocked": sum(r.status == "blocked" for r in listings),
            "not_found": sum(r.status == "not_found" for r in listings),
            "other_error": sum(r.status not in {"success", "blocked", "not_found"}
                               for r in listings),
            "elapsed_s": round(time.monotonic() - t_total, 2),
            "resolve_elapsed_s": round(resolve_elapsed, 2),
            "scrape_elapsed_s": round(stats.elapsed_s, 2) if stats else 0.0,
            "rate_per_sec": round(stats.rate, 2) if stats else 0.0,
            "cache": {
                "listings": _listing_cache.size,
                "resolutions": _resolve_cache.size,
                "hits": _listing_cache.hits + _resolve_cache.hits,
                "misses": _listing_cache.misses + _resolve_cache.misses,
            },
            "pool": _pool_stats_or_empty(request),
        },
    )


@router.post("/v2/search/listings", response_model=ScrapeResponse)
async def search_listings_v2(req: SearchListingsRequest, request: Request) -> ScrapeResponse:
    t_total = time.monotonic()
    pool = _search_pool(request)

    try:
        previews, location, search_stats = await discover_zillow_listings(
            pool=pool,
            location_query=req.location,
            bbox=req.bbox.model_dump() if req.bbox is not None else None,
            status=req.status,
            home_types=list(req.home_types),
            max_results=req.max_results,
            radius_miles=req.radius_miles,
            tile_if_needed=req.tile_if_needed,
            tile_depth=req.tile_depth,
            search_concurrency=req.search_concurrency,
            max_search_requests=req.max_search_requests,
            max_retries=req.max_retries,
            include_fsbo=req.include_fsbo,
            include_new_construction=req.include_new_construction,
            include_coming_soon=req.include_coming_soon,
            include_foreclosures=req.include_foreclosures,
            include_auctions=req.include_auctions,
            strict_location=req.strict_location,
            new_construction_only=req.new_construction_only,
            year_built_min=req.year_built_min,
            year_built_max=req.year_built_max,
            sold_in_last=req.sold_in_last,
        )
    except SearchError as e:
        raise HTTPException(502, str(e)) from e

    search_summary = {
        "total_result_count": search_stats.total_result_count,
        "requests": search_stats.search_requests,
        "tiled": search_stats.tiled,
        "discarded_out_of_location": search_stats.discarded_out_of_location,
        "discarded_out_of_area": search_stats.discarded_out_of_area,
        "discarded_missing_coordinates": search_stats.discarded_missing_coordinates,
        "coverage_limited": search_stats.coverage_limited,
        "limit_reached": search_stats.limit_reached,
        "elapsed_s": round(search_stats.elapsed_s, 2),
    }
    applied_filters = req.model_dump(include={
        "status", "home_types", "new_construction_only", "year_built_min", "year_built_max",
        "sold_in_last", "include_fsbo", "include_new_construction", "include_coming_soon",
        "include_foreclosures", "include_auctions",
    })
    if req.new_construction_only or req.status != "for_sale":
        for key in ("include_fsbo", "include_foreclosures", "include_auctions"):
            applied_filters[key] = False
    if req.status != "for_sale":
        applied_filters["include_new_construction"] = False
        applied_filters["include_coming_soon"] = False

    return ScrapeResponse(
        results=[p.to_dict() for p in previews],
        summary={
            "location": location.display if location else None,
            "area": search_stats.area,
            "applied_filters": applied_filters,
            "status": req.status,
            "home_types": list(req.home_types),
            "discovered": len(previews),
            "success": len(previews),
            "with_images": sum(1 for p in previews if p.image),
            "search": search_summary,
            "elapsed_s": round(time.monotonic() - t_total, 2),
            "search_pool": pool.stats,
        },
    )

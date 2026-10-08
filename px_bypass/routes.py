"""HTTP route handlers — kept separate from server bootstrap for clarity."""

from __future__ import annotations

import re
import time
from contextlib import aclosing
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.models import PropertyListing
from .cache import TTLCache, normalize_address
from .resolver import iter_resolved_addresses
from .scraper import parse_listing_url, scrape_urls_via_px
from .search import SearchError, SearchListingPreview, discover_zillow_listings

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


class SearchListingsRequest(BaseModel):
    location: str = Field(..., min_length=2, max_length=200)
    status: ZillowSearchStatus = "for_sale"
    home_types: list[ZillowHomeType] = Field(default_factory=lambda: ["multi_family"], min_length=1)
    max_results: int = Field(500, ge=1, le=2000)
    include_details: bool = True
    radius_miles: float = Field(12.0, ge=0.25, le=100)
    tile_if_needed: bool = True
    tile_depth: int = Field(2, ge=0, le=3)
    search_concurrency: int = Field(4, ge=1, le=16)
    concurrency: int = Field(60, ge=1, le=300)
    max_retries: int = Field(3, ge=1, le=10)
    cache_seconds: int = Field(86400, ge=0, le=86400)
    include_fsbo: bool = True
    include_new_construction: bool = True
    include_coming_soon: bool = True
    include_foreclosures: bool = False
    include_auctions: bool = False
    strict_location: bool = True


class ScrapeResponse(BaseModel):
    results: list[dict]
    summary: dict


# ─────────────── helpers ───────────────


def _pool(request: Request) -> "CookiePool":
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "Server is still booting — pool not ready.")
    if pool.stats["live"] == 0:
        raise HTTPException(503, "Cookie pool empty — all slots refreshing.")
    return pool


# ─────────────── routes ───────────────


@router.get("/info")
async def info() -> dict:
    """Describe every endpoint exposed by this service."""
    return {
        "name": "Zillow PX Bypass",
        "version": "2.0",
        "description": (
            "Scrapes Zillow listings by minting wide-scope PX cookies via "
            "patchright + Evomi residential, then replaying them concurrently."
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
                    "Discover Zillow listings by market/filter, optionally hydrate "
                    "each result through the detail scraper."
                ),
                "request": SearchListingsRequest.model_json_schema(),
                "example": {
                    "location": "Naperville, IL",
                    "status": "for_sale",
                    "home_types": ["multi_family"],
                    "max_results": 500,
                    "include_details": True,
                    "radius_miles": 12,
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
async def health(request: Request) -> dict:
    pool = request.app.state.pool
    return {
        "status": "ok" if pool.stats["live"] > 0 else "no-cookies",
        **pool.stats,
    }


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


def _merge_preview_into_detail(detail: PropertyListing, preview: SearchListingPreview) -> dict:
    data = detail.to_dict()
    preview_data = preview.to_dict()
    for key in (
        "zpid",
        "url",
        "full_address",
        "street",
        "city",
        "state",
        "zipcode",
        "price",
        "price_str",
        "bedrooms",
        "bathrooms",
        "living_area_sqft",
        "lot_size_sqft",
        "home_type",
        "listing_status",
        "days_on_zillow",
        "latitude",
        "longitude",
        "description",
    ):
        if not data.get(key) and preview_data.get(key):
            data[key] = preview_data[key]
    if not data.get("photos") and preview.image:
        data["photos"] = [preview.image]
        data["photo_count"] = max(data.get("photo_count") or 0, 1)
    data["search_preview"] = preview_data
    return data


@router.post("/v2/search/listings", response_model=ScrapeResponse)
async def search_listings_v2(req: SearchListingsRequest, request: Request) -> ScrapeResponse:
    t_total = time.monotonic()
    pool = _pool(request)

    try:
        previews, location, search_stats = await discover_zillow_listings(
            pool=pool,
            location_query=req.location,
            status=req.status,
            home_types=list(req.home_types),
            max_results=req.max_results,
            radius_miles=req.radius_miles,
            tile_if_needed=req.tile_if_needed,
            tile_depth=req.tile_depth,
            search_concurrency=req.search_concurrency,
            max_retries=req.max_retries,
            include_fsbo=req.include_fsbo,
            include_new_construction=req.include_new_construction,
            include_coming_soon=req.include_coming_soon,
            include_foreclosures=req.include_foreclosures,
            include_auctions=req.include_auctions,
            strict_location=req.strict_location,
        )
    except SearchError as e:
        raise HTTPException(502, str(e)) from e

    if not req.include_details:
        return ScrapeResponse(
            results=[p.to_dict() for p in previews],
            summary={
                "location": location.display,
                "status": req.status,
                "home_types": list(req.home_types),
                "discovered": len(previews),
                "scraped": 0,
                "success": len(previews),
                "with_images": sum(1 for p in previews if p.image),
                "include_details": False,
                "search": {
                    "total_result_count": search_stats.total_result_count,
                    "requests": search_stats.search_requests,
                    "tiled": search_stats.tiled,
                    "discarded_out_of_location": search_stats.discarded_out_of_location,
                    "elapsed_s": round(search_stats.elapsed_s, 2),
                },
                "elapsed_s": round(time.monotonic() - t_total, 2),
                "pool": _pool_stats_or_empty(request),
            },
        )

    cached_by_zpid: dict[str, PropertyListing] = {}
    misses: list[str] = []
    for preview in previews:
        hit = _listing_cache.get(preview.zpid) if req.cache_seconds > 0 else None
        if hit is not None:
            cached_by_zpid[preview.zpid] = hit
        elif preview.url:
            misses.append(preview.url)

    stats = None
    fresh: list[PropertyListing] = []
    if misses:
        fresh, stats = await scrape_urls_via_px(
            misses,
            concurrency=req.concurrency,
            max_retries=req.max_retries,
            skip_direct=True,
            pool=pool,
        )
        for result in fresh:
            if result.zpid:
                cached_by_zpid[result.zpid] = result
            if result.status == "success" and result.zpid:
                _listing_cache.set(result.zpid, result, req.cache_seconds)

    results: list[dict] = []
    for preview in previews:
        detail = cached_by_zpid.get(preview.zpid)
        if detail is not None:
            results.append(_merge_preview_into_detail(detail, preview))
        else:
            preview_data = preview.to_dict()
            preview_data["status"] = "search_only"
            preview_data["search_preview"] = preview.to_dict()
            if preview.image:
                preview_data["photos"] = [preview.image]
                preview_data["photo_count"] = 1
            results.append(preview_data)

    return ScrapeResponse(
        results=results,
        summary={
            "location": location.display,
            "status": req.status,
            "home_types": list(req.home_types),
            "discovered": len(previews),
            "scraped": len(fresh),
            "cached": len(previews) - len(misses),
            "success": sum(1 for r in results if r.get("status") == "success"),
            "blocked": stats.blocked if stats else 0,
            "not_found": stats.not_found if stats else 0,
            "other_error": stats.other_error if stats else 0,
            "with_images": sum(1 for r in results if r.get("photos")),
            "with_description": sum(1 for r in results if r.get("description")),
            "include_details": True,
            "search": {
                "total_result_count": search_stats.total_result_count,
                "requests": search_stats.search_requests,
                "tiled": search_stats.tiled,
                "discarded_out_of_location": search_stats.discarded_out_of_location,
                "elapsed_s": round(search_stats.elapsed_s, 2),
            },
            "scrape_elapsed_s": round(stats.elapsed_s, 2) if stats else 0.0,
            "rate_per_sec": round(stats.rate, 2) if stats else 0.0,
            "elapsed_s": round(time.monotonic() - t_total, 2),
            "pool": _pool_stats_or_empty(request),
        },
    )

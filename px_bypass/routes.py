"""HTTP route handlers — kept separate from server bootstrap for clarity."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.models import PropertyListing
from .cache import TTLCache, normalize_address
from .resolver import resolve_addresses
from .scraper import scrape_urls_via_px

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
    concurrency: int = Field(60, ge=1, le=300)
    max_retries: int = Field(3, ge=1, le=10)
    cache_seconds: int = Field(900, ge=0, le=86400)  # 15 min default; 0 disables


class ScrapeAddressesRequest(BaseModel):
    addresses: list[str] = Field(..., min_length=1, max_length=10000)
    concurrency: int = Field(60, ge=1, le=300)
    resolve_concurrency: int = Field(50, ge=1, le=200)
    max_retries: int = Field(3, ge=1, le=10)
    use_search_fallback: bool = True
    cache_seconds: int = Field(900, ge=0, le=86400)


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
                    "concurrency": 60,
                    "max_retries": 3,
                    "cache_seconds": 900,
                },
            },
            {
                "method": "POST",
                "path": "/v2/scrape/addresses",
                "summary": (
                    "Resolve street addresses via Zillow autocomplete, then "
                    "scrape the resulting listings."
                ),
                "request": ScrapeAddressesRequest.model_json_schema(),
                "example": {
                    "addresses": ["9315 Parrot Ave, Downey, CA 90240"],
                    "concurrency": 60,
                    "resolve_concurrency": 50,
                    "use_search_fallback": True,
                    "cache_seconds": 900,
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

    cached: list[PropertyListing] = []
    misses: list[str] = []
    for url in req.urls:
        zpid = _zpid_of(url)
        hit = _listing_cache.get(zpid) if zpid else None
        if hit is not None:
            cached.append(hit)
        else:
            misses.append(url)

    stats = None
    fresh: list[PropertyListing] = []
    if misses:
        pool = _pool(request)
        fresh, stats = await scrape_urls_via_px(
            misses, concurrency=req.concurrency, max_retries=req.max_retries,
            skip_direct=True, pool=pool,
        )
        for r in fresh:
            if r.status == "success" and r.zpid:
                _listing_cache.set(r.zpid, r, req.cache_seconds)

    results = cached + fresh
    return ScrapeResponse(
        results=[r.to_dict() for r in results],
        summary={
            "total": len(req.urls),
            "cached": len(cached),
            "fetched": len(fresh),
            "success": (stats.success if stats else 0) + len(cached),
            "blocked": stats.blocked if stats else 0,
            "not_found": stats.not_found if stats else 0,
            "other_error": stats.other_error if stats else 0,
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

    # ── 1. Resolve: cache hit returns url, miss queues for autocomplete
    addr_to_url: dict[str, str] = {}
    to_resolve: list[str] = []
    for addr in req.addresses:
        key = normalize_address(addr)
        url = _resolve_cache.get(key)
        if url:
            addr_to_url[addr] = url
        else:
            to_resolve.append(addr)

    t_resolve = time.monotonic()
    unresolved: list[str] = []
    if to_resolve:
        resolved = await resolve_addresses(
            to_resolve,
            concurrency=req.resolve_concurrency,
            use_search_fallback=req.use_search_fallback,
        )
        for addr, r in zip(to_resolve, resolved):
            if r is None:
                unresolved.append(addr)
            else:
                addr_to_url[addr] = r[2]
                _resolve_cache.set(normalize_address(addr), r[2], req.cache_seconds)
    resolve_elapsed = time.monotonic() - t_resolve

    # ── 2. Listing cache: skip URLs we already have parsed
    cached: dict[str, PropertyListing] = {}  # url -> listing
    misses_urls: list[str] = []
    for addr, url in addr_to_url.items():
        zpid = _zpid_of(url)
        hit = _listing_cache.get(zpid) if zpid else None
        if hit is not None:
            cached[url] = hit
        else:
            misses_urls.append(url)

    # ── 3. Scrape misses through the pool
    stats = None
    url_to_fresh: dict[str, PropertyListing] = {}
    if misses_urls:
        pool = _pool(request)
        fresh, stats = await scrape_urls_via_px(
            misses_urls, concurrency=req.concurrency, max_retries=req.max_retries,
            skip_direct=True, pool=pool,
        )
        for r in fresh:
            url_to_fresh[r.url or r.input_address] = r
            if r.status == "success" and r.zpid:
                _listing_cache.set(r.zpid, r, req.cache_seconds)

    # ── 4. Stitch results back into the input order
    listings: list[PropertyListing] = []
    for addr in req.addresses:
        url = addr_to_url.get(addr)
        if url is None:
            listings.append(PropertyListing(
                input_address=addr, status="not_found",
                error_msg="Address could not be resolved to a ZPID",
            ))
            continue
        r = cached.get(url) or url_to_fresh.get(url)
        if r is not None:
            r.input_address = addr
            listings.append(r)

    return ScrapeResponse(
        results=[r.to_dict() for r in listings],
        summary={
            "total": len(req.addresses),
            "resolve_cached": len(req.addresses) - len(to_resolve),
            "resolve_fetched": len(to_resolve) - len(unresolved),
            "unresolved": len(unresolved),
            "listing_cached": len(cached),
            "listing_fetched": len(url_to_fresh),
            "success": (stats.success if stats else 0) + len(cached),
            "blocked": stats.blocked if stats else 0,
            "not_found": (stats.not_found if stats else 0) + len(unresolved),
            "other_error": stats.other_error if stats else 0,
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

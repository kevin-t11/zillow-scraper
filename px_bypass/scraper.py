"""
Concurrent ForSaleNonPriorityQuery fetches via httpx.

Strategy:
  1. Try direct (no proxy) first — saves 100% bandwidth cost when our IP
     isn't rate-limited yet.
  2. On 403 / connection error, retry through Evomi residential.
  3. After 3 consecutive proxy failures too, halt with "cookies expired".

Cookies come from the cookie store (JSON file written by the refresher
daemon) on the VM, or from Brave's keychain on a laptop dev box.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import httpx

from app.models import PropertyListing
from .cookie_pool import CookiePool, PoolUnavailableError
from .parser import parse_for_sale_non_priority
from .proxy import resolve_proxy_url

logger = logging.getLogger("px_bypass.scraper")

QUERY_TEMPLATE_PATH = Path(__file__).parent / "queries" / "for_sale_non_priority.json"

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "client-id": "for-sale-sub-app-browser-client",
    "origin": "https://www.zillow.com",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-enabled-listings-flag": "restricted",
    "x-z-enable-oauth-conversion": "true",
}

# Matches both slug+zpid (`/homedetails/<slug>/<zpid>_zpid/`) and
# slug-less (`/homedetails/<zpid>_zpid/`) variants — autocomplete returns
# the latter, real listing pages return the former.
URL_RE = re.compile(r"/(?:homedetails|community|b)/(?:([^/]+)/)?(\d+)_zpid/?")


def parse_listing_url(url: str) -> tuple[str, str]:
    m = URL_RE.search(url)
    if not m:
        raise ValueError(f"not a zillow listing URL: {url}")
    slug = m.group(1) or "x"  # placeholder for the referer header
    return slug, m.group(2)


@dataclass
class ScrapeStats:
    total: int = 0
    success: int = 0
    blocked: int = 0
    not_found: int = 0
    other_error: int = 0
    direct_success: int = 0
    proxy_success: int = 0
    bytes_total: int = 0
    elapsed_s: float = 0.0

    @property
    def rate(self) -> float:
        return self.total / self.elapsed_s if self.elapsed_s else 0.0


@dataclass
class _State:
    """Shared cross-task state for fail-fast on dead cookies."""
    consecutive_proxy_blocks: int = 0
    halted: bool = False
    direct_disabled_until: float = 0.0  # disable direct path if it's getting blocked

    def record_proxy_block(self) -> None:
        self.consecutive_proxy_blocks += 1
        if self.consecutive_proxy_blocks >= 3 and not self.halted:
            self.halted = True
            logger.error(
                "px_bypass: 3 consecutive 403s through PROXY — cookies expired. "
                "Refresher will mint fresh ones; rerun shortly."
            )

    def record_proxy_success(self) -> None:
        self.consecutive_proxy_blocks = 0

    def disable_direct_for(self, seconds: float) -> None:
        self.direct_disabled_until = time.monotonic() + seconds

    @property
    def direct_ok(self) -> bool:
        return time.monotonic() >= self.direct_disabled_until


def _build_request(zpid: str, slug: str, query_template: dict) -> tuple[str, dict, dict]:
    url = (
        f"https://www.zillow.com/graphql/?zpid={zpid}"
        "&platform=DESKTOP_WEB&formType=OPAQUE"
        "&contactFormRenderParameter=&skipCFRD=false"
        "&submitFlowRequestSource=&ompDeviceType=WEB_DESKTOP"
        "&operationName=ForSaleNonPriorityQuery"
    )
    # Only these two variable fields change; the safelisted query stays exact.
    body = dict(query_template)
    body["variables"] = dict(query_template["variables"])
    body["variables"]["contactFormRenderParameter"] = dict(
        query_template["variables"]["contactFormRenderParameter"]
    )
    body["variables"]["zpid"] = int(zpid)
    body["variables"]["contactFormRenderParameter"]["zpid"] = int(zpid)
    headers = dict(HEADERS)
    headers["referer"] = f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
    return url, body, headers


async def _attempt(
    client: httpx.AsyncClient, url: str, body: dict, headers: dict, cookies: dict,
) -> tuple[int, bytes]:
    """Single POST. Returns (status, body_bytes)."""
    r = await client.post(url, json=body, headers=headers, timeout=30,
                          **({"cookies": cookies} if cookies else {}))
    return r.status_code, r.content


async def _fetch_one(
    direct_client: httpx.AsyncClient,
    proxy_client: Optional[httpx.AsyncClient],
    listing_url: str,
    cookies: dict,
    query_template: dict,
    sem: asyncio.Semaphore,
    state: _State,
    max_retries: int = 3,
    skip_direct: bool = False,
    pool: Optional[CookiePool] = None,
) -> tuple[PropertyListing, int, str]:
    if pool is not None and state.halted:
        try:
            _, zpid = parse_listing_url(listing_url)
        except ValueError:
            zpid = ""
        return PropertyListing(input_address=listing_url, url=listing_url, zpid=zpid,
                               status="blocked", error_msg="No validated scraping session is available"), 0, "fail"
    try:
        return await _fetch_one_impl(direct_client, proxy_client, listing_url,
                                     cookies, query_template, sem, state,
                                     max_retries, skip_direct, pool)
    except PoolUnavailableError as exc:
        state.halted = True
        try:
            _, zpid = parse_listing_url(listing_url)
        except ValueError:
            zpid = ""
        return PropertyListing(input_address=listing_url, url=listing_url, zpid=zpid,
                               status="blocked", error_msg=str(exc)), 0, "fail"


def _parse_requested_listing(content: bytes, listing_url: str, zpid: str) -> PropertyListing:
    try:
        payload = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise ValueError("response was not valid JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    prop = data.get("property") if isinstance(data, dict) else None
    if not isinstance(prop, dict) or str(prop.get("zpid")) != zpid:
        raise ValueError("response did not contain the requested property")
    return parse_for_sale_non_priority(payload, listing_url=listing_url)


async def _fetch_one_impl(
    direct_client: httpx.AsyncClient, proxy_client: Optional[httpx.AsyncClient],
    listing_url: str, cookies: dict, query_template: dict, sem: asyncio.Semaphore,
    state: _State, max_retries: int = 3, skip_direct: bool = False,
    pool: Optional[CookiePool] = None,
) -> tuple[PropertyListing, int, str]:
    """Bound every network attempt, including rejected and malformed responses."""
    try:
        slug, zpid = parse_listing_url(listing_url)
    except ValueError as exc:
        return PropertyListing(input_address=listing_url, status="error", error_msg=str(exc)), 0, "fail"

    url, body, headers = _build_request(zpid, slug, query_template)
    last_err = ""
    attempts = 0
    pool_misses = 0
    terminal_status = None
    while attempts < max_retries and pool_misses < max_retries * 4:
        if state.halted:
            terminal_status = "blocked"
            last_err = last_err or "No validated scraping session is available"
            break
        lease = pool.lease() if pool is not None else contextlib.nullcontext(None)
        try:
            async with lease as slot:
                active_proxy_client = slot.client if slot is not None else proxy_client
                active_sem = slot.sem if slot is not None else sem
                if slot is not None:
                    cookies = slot.cookies
                async with (sem if pool is not None else contextlib.nullcontext()), active_sem:
                    # Expiration can happen while waiting for either semaphore.
                    if slot is not None and not pool.usable(slot):
                        pool.mark_dead(slot, reason="session expired or rejected while queued")
                        pool_misses += 1
                        continue
                    clients = []
                    if state.direct_ok and not skip_direct:
                        clients.append(("direct", direct_client))
                    if active_proxy_client is not None:
                        clients.append(("proxy", active_proxy_client))
                    if not clients:
                        last_err = "No request transport is available"
                        break
                    for path, client in clients:
                        if attempts >= max_retries:
                            break
                        attempts += 1
                        try:
                            status, content = await _attempt(client, url, body, headers, cookies)
                        except Exception as exc:
                            last_err = f"{path} {type(exc).__name__}"
                            if path == "direct":
                                state.disable_direct_for(30.0)
                            continue
                        if status == 200:
                            try:
                                listing = _parse_requested_listing(content, listing_url, zpid)
                            except ValueError as exc:
                                last_err = f"{path}: {exc}"
                                continue
                            listing.url = listing.url or listing_url
                            listing.attempts = attempts
                            if path == "proxy":
                                state.record_proxy_success()
                                if slot is not None:
                                    slot.last_success_at = time.time()
                            return listing, len(content), path
                        last_err = f"{path} HTTP {status}"
                        if status == 404:
                            terminal_status = "not_found"
                            break
                        if status == 403:
                            if path == "direct":
                                state.disable_direct_for(60.0)
                            else:
                                if pool is not None and slot is not None:
                                    pool.mark_dead(slot)
                                else:
                                    state.record_proxy_block()
                                break
                    if terminal_status:
                        break
        except PoolUnavailableError as exc:
            state.halted = True
            terminal_status = "blocked"
            last_err = f"{last_err}; {exc}" if last_err else str(exc)
            break

    status = terminal_status or ("blocked" if "403" in last_err else "error")
    return PropertyListing(
        input_address=listing_url, status=status, error_msg=last_err,
        attempts=attempts, zpid=zpid, url=listing_url,
    ), 0, "fail"



async def scrape_urls_via_px(
    urls: Iterable[str] | AsyncIterable[str],
    *,
    cookies: Optional[dict] = None,
    proxy_url: Optional[str] = None,
    proxy_backend: str = "evomi",
    use_proxy_fallback: bool = True,
    skip_direct: bool = False,
    concurrency: int = 50,
    max_retries: int = 3,
    pool: Optional[CookiePool] = None,
) -> tuple[list[PropertyListing], ScrapeStats]:

    if pool is None and (cookies is None or "_px3" not in cookies):
        raise SystemExit("Pass either pool=CookiePool or cookies={'_px3': ...}")
    cookies = cookies or {}

    proxy_url = proxy_url or (
        resolve_proxy_url(proxy_backend) if use_proxy_fallback and pool is None else ""
    )
    logger.info(
        "Cookies=%d  proxy_fallback=%s",
        len(cookies),
        proxy_url.split("@")[1] if (proxy_url and "@" in proxy_url) else "none",
    )

    query_template = json.loads(QUERY_TEMPLATE_PATH.read_text())
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    sem = asyncio.Semaphore(concurrency)
    state = _State()
    stats = ScrapeStats()
    t0 = time.monotonic()

    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )

    direct_ctx = httpx.AsyncClient(http2=True, follow_redirects=True, limits=limits)
    proxy_ctx = httpx.AsyncClient(
        http2=True, follow_redirects=True, proxy=proxy_url, limits=limits,
    ) if proxy_url else None

    results: list[PropertyListing] = []
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=concurrency)

    async def _produce():
        if isinstance(urls, AsyncIterable):
            iterator = aiter(urls)
            try:
                async for url in iterator:
                    await queue.put(url)
                    stats.total += 1
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
        else:
            for url in urls:
                await queue.put(url)
                stats.total += 1
        for _ in range(concurrency):
            await queue.put(None)

    async with direct_ctx as direct_client:
        async def _worker():
            while True:
                url = await queue.get()
                if url is None:
                    return
                listing, size, path = await _fetch_one(
                    direct_client, proxy_ctx, url, cookies, query_template,
                    sem, state, max_retries, skip_direct, pool,
                )
                results.append(listing)
                stats.bytes_total += size
                if path == "direct":
                    stats.direct_success += 1
                elif path == "proxy":
                    stats.proxy_success += 1
                if listing.status == "success":
                    stats.success += 1
                elif listing.status == "blocked":
                    stats.blocked += 1
                elif listing.status == "not_found":
                    stats.not_found += 1
                else:
                    stats.other_error += 1

        tasks = [asyncio.create_task(_produce())]
        tasks.extend(asyncio.create_task(_worker()) for _ in range(concurrency))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proxy_ctx is not None:
                await proxy_ctx.aclose()

    stats.elapsed_s = time.monotonic() - t0
    return results, stats

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
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import httpx

from app.models import PropertyListing
from .cookie_pool import CookiePool
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
    body = json.loads(json.dumps(query_template))
    body["variables"]["zpid"] = int(zpid)
    body["variables"]["contactFormRenderParameter"]["zpid"] = int(zpid)
    headers = dict(HEADERS)
    headers["referer"] = f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
    return url, body, headers


async def _attempt(
    client: httpx.AsyncClient, url: str, body: dict, headers: dict, cookies: dict,
) -> tuple[int, bytes]:
    """Single POST. Returns (status, body_bytes)."""
    r = await client.post(url, json=body, headers=headers, cookies=cookies, timeout=30)
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
    """Returns (listing, bytes_received, path: 'direct'|'proxy'|'fail')."""
    try:
        slug, zpid = parse_listing_url(listing_url)
    except ValueError as e:
        return PropertyListing(
            input_address=listing_url, status="error", error_msg=str(e),
        ), 0, "fail"

    url, body, headers = _build_request(zpid, slug, query_template)
    last_err = ""

    real_attempts = 0
    pool_misses = 0
    attempt = 0
    while real_attempts < max_retries and pool_misses < max_retries * 4:
        attempt += 1
        if pool is None and state.halted:
            return PropertyListing(
                input_address=listing_url, status="blocked",
                error_msg="halted: cookies expired",
                attempts=attempt - 1, zpid=zpid, url=listing_url,
            ), 0, "fail"

        slot = None
        active_proxy_client = proxy_client
        active_sem = sem
        if pool is not None:
            slot = await pool.acquire()
            cookies = slot.cookies
            active_proxy_client = slot.client
            active_sem = slot.sem

        async with active_sem:
            # ── Try DIRECT first (free) when not rate-limited
            if state.direct_ok and not skip_direct:
                try:
                    status, content = await _attempt(direct_client, url, body, headers, cookies)
                    if status == 200:
                        try:
                            j = json.loads(content)
                        except Exception as e:
                            last_err = f"direct JSON parse: {e}"; continue
                        if "errors" in j and "data" not in j:
                            last_err = (j.get('errors') or [{}])[0].get('message', '')
                            continue
                        listing = parse_for_sale_non_priority(j, listing_url=listing_url)
                        listing.attempts = attempt
                        return listing, len(content), "direct"
                    if status == 403:
                        # Direct rate-limited — disable direct path for 60s
                        state.disable_direct_for(60.0)
                        last_err = "direct 403"
                        # fall through to proxy attempt
                    elif status == 404:
                        last_err = "direct 404"
                        break
                    else:
                        last_err = f"direct HTTP {status}"
                except Exception as e:
                    last_err = f"direct {type(e).__name__}: {e}"
                    state.disable_direct_for(30.0)

            # ── Fall back to PROXY (Evomi residential)
            if active_proxy_client is None:
                continue
            try:
                status, content = await _attempt(active_proxy_client, url, body, headers, cookies)
                if status == 200:
                    try:
                        j = json.loads(content)
                    except Exception as e:
                        last_err = f"proxy JSON parse: {e}"; continue
                    if "errors" in j and "data" not in j:
                        last_err = (j.get('errors') or [{}])[0].get('message', '')
                        continue
                    listing = parse_for_sale_non_priority(j, listing_url=listing_url)
                    listing.attempts = attempt
                    state.record_proxy_success()
                    return listing, len(content), "proxy"
                if status == 403:
                    last_err = "proxy 403"
                    if pool is not None and slot is not None:
                        pool.mark_dead(slot)
                        pool_misses += 1
                        continue  # don't count as a real attempt
                    state.record_proxy_block()
                    break
                if status == 404:
                    last_err = "proxy 404"
                    break
                last_err = f"proxy HTTP {status}"
            except Exception as e:
                last_err = f"proxy {type(e).__name__}: {e}"
            real_attempts += 1

    status = (
        "blocked" if "403" in last_err
        else "not_found" if "404" in last_err
        else "error"
    )
    return PropertyListing(
        input_address=listing_url, status=status, error_msg=last_err,
        attempts=max_retries, zpid=zpid, url=listing_url,
    ), 0, "fail"




async def scrape_urls_via_px(
    urls: Iterable[str],
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

    proxy_url = proxy_url or (resolve_proxy_url(proxy_backend) if use_proxy_fallback else "")
    logger.info(
        "Cookies=%d  proxy_fallback=%s",
        len(cookies),
        proxy_url.split("@")[1] if (proxy_url and "@" in proxy_url) else "none",
    )

    query_template = json.loads(QUERY_TEMPLATE_PATH.read_text())
    urls_list = list(urls)
    sem = asyncio.Semaphore(concurrency)
    state = _State()
    stats = ScrapeStats(total=len(urls_list))
    t0 = time.monotonic()

    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )

    direct_ctx = httpx.AsyncClient(http2=True, follow_redirects=True, limits=limits)
    proxy_ctx = httpx.AsyncClient(
        http2=True, follow_redirects=True, proxy=proxy_url, limits=limits,
    ) if proxy_url else None

    async with direct_ctx as direct_client:
        try:
            tasks = [
                _fetch_one(
                    direct_client, proxy_ctx, u, cookies, query_template,
                    sem, state, max_retries, skip_direct, pool,
                )
                for u in urls_list
            ]
            results: list[PropertyListing] = []
            for fut in asyncio.as_completed(tasks):
                listing, size, path = await fut
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
        finally:
            if proxy_ctx is not None:
                await proxy_ctx.aclose()

    stats.elapsed_s = time.monotonic() - t0
    return results, stats

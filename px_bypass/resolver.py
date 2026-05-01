"""
Address → ZPID URL resolver.

Wraps the free Zillow autocomplete API (no PX, no proxy). For addresses
that fail autocomplete we fall back to Zillow homes search. Both helpers
already exist in app/scraper.py — we re-use them here so the v2 path
doesn't fork the resolution logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional

import httpx

from app.scraper import (
    get_zpid_via_autocomplete,
    get_zpid_via_zillow_search,
)

logger = logging.getLogger("px_bypass.resolver")


async def resolve_one(
    client: httpx.AsyncClient, address: str, *, use_search_fallback: bool = True,
) -> Optional[tuple[str, str, str]]:
    """Returns (input_address, zpid, url) or None."""
    try:
        r = await get_zpid_via_autocomplete(client, address)
        if r:
            return (address, r[0], r[1])
    except Exception as e:
        logger.debug("autocomplete error for %r: %s", address, e)

    if use_search_fallback:
        try:
            r = await get_zpid_via_zillow_search(client, address)
            if r:
                return (address, r[0], r[1])
        except Exception as e:
            logger.debug("search error for %r: %s", address, e)
    return None


async def resolve_addresses(
    addresses: Iterable[str],
    *,
    concurrency: int = 50,
    use_search_fallback: bool = True,
) -> list[Optional[tuple[str, str, str]]]:
    """Resolve all addresses in parallel. Order matches input."""
    sem = asyncio.Semaphore(concurrency)
    addr_list = list(addresses)

    async with httpx.AsyncClient(http2=True, timeout=30, follow_redirects=True) as client:
        async def _bound(addr):
            async with sem:
                return await resolve_one(client, addr, use_search_fallback=use_search_fallback)

        return await asyncio.gather(*[_bound(a) for a in addr_list])

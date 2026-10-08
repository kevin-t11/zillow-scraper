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
from collections.abc import AsyncIterator
from typing import Iterable, Optional

import httpx

from app.scraper import (
    get_address_unit,
    get_zpid_via_autocomplete,
    get_zpid_via_zillow_search,
)

logger = logging.getLogger("px_bypass.resolver")


async def resolve_one(
    client: httpx.AsyncClient, address: str, *, use_search_fallback: bool = True,
) -> Optional[tuple[str, str, str]]:
    """Returns (input_address, zpid, url) or None."""
    try:
        r = await get_zpid_via_autocomplete(client, address, prefer_full_address=True)
        if r:
            return (address, r[0], r[1])
    except Exception as e:
        logger.debug("autocomplete error for %r: %s", address, e)

    # Search returns its first property without proving the unit identity.
    if use_search_fallback and not get_address_unit(address):
        try:
            r = await get_zpid_via_zillow_search(client, address)
            if r:
                return (address, r[0], r[1])
        except Exception as e:
            logger.debug("search error for %r: %s", address, e)
    return None


async def iter_resolved_addresses(
    addresses: Iterable[str],
    *,
    concurrency: int = 50,
    use_search_fallback: bool = True,
) -> AsyncIterator[tuple[int, Optional[tuple[str, str, str]]]]:
    """Yield (input index, resolution) as each address finishes."""
    sem = asyncio.Semaphore(concurrency)
    addr_list = list(addresses)
    if not addr_list:
        return

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        http2=True, timeout=30, follow_redirects=True, limits=limits,
    ) as client:
        async def _bound(index, addr):
            async with sem:
                return index, await resolve_one(client, addr, use_search_fallback=use_search_fallback)

        tasks = [asyncio.create_task(_bound(i, a)) for i, a in enumerate(addr_list)]
        try:
            for task in asyncio.as_completed(tasks):
                yield await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def resolve_addresses(
    addresses: Iterable[str], *, concurrency: int = 50,
    use_search_fallback: bool = True,
) -> list[Optional[tuple[str, str, str]]]:
    """Resolve all addresses in parallel. Order matches input."""
    addr_list = list(addresses)
    results = [None] * len(addr_list)
    async for index, resolved in iter_resolved_addresses(
        addr_list, concurrency=concurrency, use_search_fallback=use_search_fallback,
    ):
        results[index] = resolved
    return results

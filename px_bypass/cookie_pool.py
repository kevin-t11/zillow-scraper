"""
Cookie pool — rotates across N independent PX cookie sets so we can
sustain bulk scrapes past the per-cookie wear limit.

Each slot pairs a PX cookie set with a sticky Evomi session (one exit
IP for the cookie's lifetime) and a per-slot concurrency cap, so PX
sees a serial-ish stream from one IP per session — the cookie lasts
much longer than under concurrent multi-IP churn.

On a 403 the slot is marked dead and a background task refreshes it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from .proxy import EvomiConfig, build_proxy_url, make_session_id

Minter = Callable[[], Awaitable[dict]]

logger = logging.getLogger("px_bypass.cookie_pool")

DEFAULT_PERSIST_PATH = Path(
    os.getenv("COOKIE_POOL_PERSIST", str(Path.home() / ".cache/zillow_pool/slots.json"))
)
DEFAULT_PERSIST_TTL_S = float(os.getenv("COOKIE_POOL_TTL_S", "21600"))  # 6 hours


@dataclass
class CookieSlot:
    idx: int
    cookies: dict
    session_id: str
    client: httpx.AsyncClient  # has sticky Evomi proxy baked in
    sem: asyncio.Semaphore     # per-slot concurrency cap
    minted_at: float
    hits: int = 0
    dead: bool = False
    refreshing: bool = False


class CookiePool:
    def __init__(
        self,
        mint: Minter,
        size: int,
        *,
        per_slot_concurrency: int = 5,
        evomi_cfg: Optional[EvomiConfig] = None,
        persist_path: Optional[Path] = None,
        persist_ttl_s: float = DEFAULT_PERSIST_TTL_S,
    ):
        self._mint_fn = mint
        self._size = size
        self._per_slot_c = per_slot_concurrency
        self._evomi = evomi_cfg or EvomiConfig.from_env()
        self._slots: list[Optional[CookieSlot]] = [None] * size
        self._lock = asyncio.Lock()
        self._refreshed = asyncio.Event()
        self._refreshed.set()
        self._cursor = 0
        self._mints = 0
        self._refreshes = 0
        self._persist_path = persist_path or DEFAULT_PERSIST_PATH
        self._persist_ttl_s = persist_ttl_s
        self._rehydrated = 0

    @classmethod
    async def create(
        cls,
        mint: Minter,
        size: int,
        *,
        per_slot_concurrency: int = 5,
        persist_path: Optional[Path] = None,
        persist_ttl_s: float = DEFAULT_PERSIST_TTL_S,
    ) -> "CookiePool":
        pool = cls(
            mint, size,
            per_slot_concurrency=per_slot_concurrency,
            persist_path=persist_path,
            persist_ttl_s=persist_ttl_s,
        )

        # ── Try to rehydrate from disk first
        rehydrated = pool._rehydrate_from_disk()
        if rehydrated:
            logger.info("rehydrated %d/%d slots from %s — minting %d more",
                        rehydrated, size, pool._persist_path, size - rehydrated)
        else:
            logger.info("minting initial cookie pool of %d…", size)

        # Mint any slot that wasn't rehydrated, retry failed slots up to 2x
        # so boot is deterministic at full pool size in prod.
        for attempt in range(3):
            idxs_to_mint = [i for i in range(size) if pool._slots[i] is None]
            if not idxs_to_mint:
                break
            results = await asyncio.gather(
                *[pool._mint(i) for i in idxs_to_mint], return_exceptions=True,
            )
            new_live = sum(1 for r in results if isinstance(r, CookieSlot))
            logger.info("mint attempt %d: %d/%d slots succeeded",
                        attempt + 1, new_live, len(idxs_to_mint))
            if new_live == len(idxs_to_mint):
                break

        live = sum(1 for s in pool._slots if s is not None and not s.dead)
        if live == 0:
            raise RuntimeError("cookie pool: failed to mint any slots")
        logger.info("cookie pool ready: %d/%d live (rehydrated=%d, fresh=%d)",
                    live, size, rehydrated, pool._mints)
        pool._persist_to_disk()
        return pool

    # ── Persistence ─────────────────────────────────────────────────

    def _rehydrate_from_disk(self) -> int:
        """Load slot cookies+session_ids from disk. Returns count rehydrated."""
        try:
            if not self._persist_path.exists():
                return 0
            payload = json.loads(self._persist_path.read_text())
        except Exception as e:
            logger.warning("rehydrate skipped: %s", e)
            return 0

        now = time.time()
        rehydrated = 0
        for entry in payload.get("slots", []):
            idx = entry.get("idx")
            minted_at = entry.get("minted_at", 0)
            cookies = entry.get("cookies") or {}
            session_id = entry.get("session_id")
            if (idx is None or idx >= self._size or "_px3" not in cookies
                    or now - minted_at > self._persist_ttl_s):
                continue
            client = self._build_client(session_id or make_session_id())
            self._slots[idx] = CookieSlot(
                idx=idx, cookies=cookies, session_id=session_id or make_session_id(),
                client=client, sem=asyncio.Semaphore(self._per_slot_c),
                minted_at=minted_at,
            )
            self._rehydrated += 1
            rehydrated += 1
        return rehydrated

    def _persist_to_disk(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": time.time(),
                "slots": [
                    {
                        "idx": s.idx,
                        "minted_at": s.minted_at,
                        "session_id": s.session_id,
                        "cookies": s.cookies,
                    }
                    for s in self._slots if s is not None and not s.dead
                ],
            }
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._persist_path)
        except Exception as e:
            logger.warning("persist failed: %s", e)

    def _build_client(self, session_id: str) -> httpx.AsyncClient:
        proxy_url = build_proxy_url(
            self._evomi, session=session_id, country="US", lifetime_min=30,
        )
        limits = httpx.Limits(
            max_connections=self._per_slot_c * 2,
            max_keepalive_connections=self._per_slot_c,
        )
        return httpx.AsyncClient(
            http2=True, follow_redirects=True, proxy=proxy_url, limits=limits,
        )

    async def _mint(self, idx: int) -> CookieSlot:
        old = self._slots[idx]
        try:
            cookies = await self._mint_fn()
        except Exception as e:
            logger.warning("pool[%d] mint failed: %s", idx, e)
            self._slots[idx] = None
            raise
        if "JSESSIONID" not in cookies:
            self._slots[idx] = None
            raise RuntimeError(f"pool[{idx}] no usable session in {list(cookies)}")
        if "_px3" not in cookies:
            logger.info("pool[%d] minted without _px3 (PX not challenging)", idx)

        session_id = make_session_id()
        client = self._build_client(session_id)
        slot = CookieSlot(
            idx=idx, cookies=cookies, session_id=session_id,
            client=client, sem=asyncio.Semaphore(self._per_slot_c),
            minted_at=time.time(),
        )
        self._slots[idx] = slot
        self._mints += 1
        # close old client lazily — workers may still hold it
        if old is not None:
            asyncio.create_task(_close_later(old.client))
        return slot

    async def acquire(self) -> CookieSlot:
        """Round-robin pick a live slot. Awaits a refresh if all are dead."""
        while True:
            async with self._lock:
                for _ in range(self._size):
                    self._cursor = (self._cursor + 1) % self._size
                    s = self._slots[self._cursor]
                    if s is not None and not s.dead:
                        s.hits += 1
                        return s
                self._refreshed.clear()
            await self._refreshed.wait()

    def mark_dead(self, slot: CookieSlot) -> None:
        if slot.dead:
            return
        slot.dead = True
        logger.info("pool[%d] dead after %d hits — scheduling refresh", slot.idx, slot.hits)
        asyncio.create_task(self._refresh(slot.idx))

    async def _refresh(self, idx: int) -> None:
        cur = self._slots[idx]
        if cur is not None and cur.refreshing:
            return
        if cur is not None:
            cur.refreshing = True
        try:
            await self._mint(idx)
            self._refreshes += 1
            self._persist_to_disk()
            logger.info("pool[%d] refreshed", idx)
        except Exception as e:
            logger.warning("pool[%d] refresh failed: %s", idx, e)
        finally:
            self._refreshed.set()

    async def aclose(self) -> None:
        await asyncio.gather(
            *[s.client.aclose() for s in self._slots if s is not None],
            return_exceptions=True,
        )

    @property
    def stats(self) -> dict:
        live = sum(1 for s in self._slots if s and not s.dead)
        return {
            "size": self._size,
            "live": live,
            "mints": self._mints,
            "refreshes": self._refreshes,
            "rehydrated": self._rehydrated,
        }


async def _close_later(client: httpx.AsyncClient) -> None:
    try:
        await asyncio.sleep(60)
        await client.aclose()
    except Exception:
        pass

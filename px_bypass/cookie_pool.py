"""Validated Evomi HTTP sessions, with optional legacy browser session setup.

Ordinary HTTP sessions use the server's cookie jar. Importing browser risk
cookies can turn an otherwise successful public listing request into a 403.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import certifi
import httpx

from .proxy import EvomiConfig, build_proxy_url, make_session_id

Minter = Callable[..., Awaitable[dict]]

logger = logging.getLogger("px_bypass.cookie_pool")

DEFAULT_PERSIST_PATH = Path(
    os.getenv("COOKIE_POOL_PERSIST", str(Path.home() / ".cache/zillow_pool/slots.json"))
)
DEFAULT_PERSIST_TTL_S = float(os.getenv("COOKIE_POOL_TTL_S", "1500"))  # shorter than the 30 minute proxy session


class PoolUnavailableError(RuntimeError):
    """No validated session became available within the request's wait budget."""


class SessionValidationError(RuntimeError):
    """A cookie session could not fetch the validation property."""


def _failure_reason(exc: Exception) -> str:
    # Only our own controlled validation messages are safe to expose.
    if isinstance(exc, SessionValidationError):
        return str(exc)
    return f"Session setup failed: {type(exc).__name__}"


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
    pending: int = 0
    validated: bool = False
    last_success_at: float | None = None


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
        session_wait_s: float = 30.0,
        retry_delay_s: float = 60.0,
        prefer_http_sessions: bool = True,
    ):
        self._mint_fn = mint
        self._prefer_http_sessions = prefer_http_sessions
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
        self._session_wait_s = session_wait_s
        self._retry_delay_s = retry_delay_s
        self._retry_after = 0.0
        self._recovery_lock = asyncio.Lock()
        self._refreshing_indices: set[int] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._validation_failures = 0
        self._last_error: str | None = None
        self._recovery_active = False
        self._last_recovery_started_at: float | None = None
        self._last_recovery_completed_at: float | None = None
        self._proxy_ssl_context: ssl.SSLContext | None = None

    @classmethod
    async def create(
        cls,
        mint: Minter,
        size: int,
        *,
        per_slot_concurrency: int = 5,
        persist_path: Optional[Path] = None,
        persist_ttl_s: float = DEFAULT_PERSIST_TTL_S,
        prefer_http_sessions: bool = True,
    ) -> "CookiePool":
        pool = cls(
            mint, size,
            per_slot_concurrency=per_slot_concurrency,
            persist_path=persist_path,
            persist_ttl_s=persist_ttl_s,
            prefer_http_sessions=prefer_http_sessions,
        )

        # ── Try to rehydrate from disk first
        rehydrated = pool._rehydrate_from_disk()
        if rehydrated:
            logger.info("rehydrated %d/%d slots from %s — minting %d more",
                        rehydrated, size, pool._persist_path, size - rehydrated)
        else:
            logger.info("minting initial cookie pool of %d…", size)

        # Persisted cookies also need proof of access before becoming usable.
        for slot in list(pool._slots):
            if slot is None:
                continue
            try:
                await pool._validate_slot(slot)
                slot.validated = True
            except Exception:
                await slot.client.aclose()
                pool._slots[slot.idx] = None

        # Probe one fresh session before spending bandwidth on an entire pool.
        if not any(pool._slots):
            try:
                await pool._mint(0)
            except Exception as exc:
                pool._last_error = _failure_reason(exc)
                pool._retry_after = time.monotonic() + pool._retry_delay_s
                pool._schedule_refresh(0)
                pool._spawn(pool._maintain())
                logger.warning("no validated sessions; API will report unavailable and retry with backoff")
                return pool

        # Serve after the first validated slot; fill the pool with bounded,
        # serialized background recovery instead of blocking startup for minutes.
        live = sum(1 for s in pool._slots if s is not None and not s.dead)
        logger.info("cookie pool ready: %d/%d live (rehydrated=%d, fresh=%d)",
                    live, size, rehydrated, pool._mints)
        pool._persist_to_disk()
        pool._schedule_missing()
        pool._spawn(pool._maintain())
        return pool

    async def _validate_slot(self, slot: CookieSlot) -> None:
        # Import lazily: scraper imports CookiePool for its worker interface.
        from .scraper import QUERY_TEMPLATE_PATH, _attempt, _build_request
        zpid = "51816594"  # the same property used to establish the browser session
        url, body, headers = _build_request(zpid, "6910-Farragut-Ave-Falls-Church-VA-22042", json.loads(QUERY_TEMPLATE_PATH.read_text()))
        status, content = await _attempt(slot.client, url, body, headers, slot.cookies, timeout=15)
        if status != 200:
            raise SessionValidationError(f"Validation listing returned HTTP {status}")
        try:
            prop = (json.loads(content).get("data") or {}).get("property") or {}
        except (ValueError, AttributeError, TypeError):
            prop = {}
        if not isinstance(prop, dict) or str(prop.get("zpid")) != zpid:
            raise SessionValidationError("Validation response did not contain the requested property")
        slot.last_success_at = time.time()

    def usable(self, slot: CookieSlot, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (slot.validated and not slot.dead
                and 0 <= now - slot.minted_at < self._persist_ttl_s)

    def _expire_slots(self) -> None:
        now = time.time()
        for slot in self._slots:
            if slot is not None and not slot.dead and now - slot.minted_at >= self._persist_ttl_s:
                self.mark_dead(slot, reason="session age limit")

    async def _maintain(self) -> None:
        while not self._closing:
            self._expire_slots()
            await asyncio.sleep(min(15.0, max(0.01, self._persist_ttl_s / 2)))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _schedule_refresh(self, idx: int) -> None:
        if self._closing or idx in self._refreshing_indices:
            return
        self._refreshing_indices.add(idx)
        self._spawn(self._refresh(idx))

    def _schedule_missing(self) -> None:
        for idx, slot in enumerate(self._slots):
            if slot is None:
                self._schedule_refresh(idx)

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
            if (not isinstance(idx, int) or idx < 0 or idx >= self._size
                    or not isinstance(cookies, dict) or not session_id
                    or now - minted_at > self._persist_ttl_s):
                continue
            if self._prefer_http_sessions and "_px3" in cookies:
                continue
            client = self._build_client(session_id)
            self._slots[idx] = CookieSlot(
                idx=idx, cookies=cookies, session_id=session_id,
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
                    for s in self._slots if s is not None and self.usable(s)
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
        if self._evomi.scheme == "https" and self._proxy_ssl_context is None:
            # Match httpcore's verified proxy TLS defaults, but load the trust
            # store once. Its implicit default rebuilds this synchronously for
            # every new proxy connection, stalling bulk fetches on the event loop.
            self._proxy_ssl_context = ssl.create_default_context()
            self._proxy_ssl_context.load_verify_locations(certifi.where())
        proxy = httpx.Proxy(proxy_url, ssl_context=self._proxy_ssl_context)
        limits = httpx.Limits(
            max_connections=self._per_slot_c * 2,
            max_keepalive_connections=self._per_slot_c,
        )
        return httpx.AsyncClient(
            http2=True, follow_redirects=True, proxy=proxy, limits=limits,
        )

    async def _mint(self, idx: int) -> CookieSlot:
        old = self._slots[idx]
        session_id = make_session_id()
        started_at = time.time()
        try:
            cookies = {} if self._prefer_http_sessions else await self._mint_fn(session_id=session_id)
        except Exception as e:
            logger.warning("pool[%d] mint failed (%s)", idx, type(e).__name__)
            self._slots[idx] = None
            if old is not None:
                self._spawn(_close_later(old.client))
            raise
        if not self._prefer_http_sessions and "JSESSIONID" not in cookies and "_px3" not in cookies:
            self._slots[idx] = None
            if old is not None:
                self._spawn(_close_later(old.client))
            raise RuntimeError(f"pool[{idx}] no usable session in {list(cookies)}")
        client = self._build_client(session_id)
        slot = CookieSlot(
            idx=idx, cookies=cookies, session_id=session_id,
            client=client, sem=asyncio.Semaphore(self._per_slot_c),
            minted_at=started_at,
        )
        try:
            await self._validate_slot(slot)
        except BaseException as exc:
            await client.aclose()
            if isinstance(exc, Exception):
                self._validation_failures += 1
                self._last_error = _failure_reason(exc)
            self._slots[idx] = None
            if old is not None:
                self._spawn(_close_later(old.client))
            raise
        slot.validated = True
        self._slots[idx] = slot
        self._mints += 1
        # close old client lazily — workers may still hold it
        if old is not None:
            self._spawn(_close_later(old.client))
        self._last_error = None
        return slot

    async def acquire(self, *, reserve: bool = False) -> CookieSlot:
        """Pick a live slot; leases reserve load, existing raw callers do not."""
        deadline = time.monotonic() + self._session_wait_s
        while not self._closing:
            async with self._lock:
                self._expire_slots()
                candidates = []
                for _ in range(self._size):
                    self._cursor = (self._cursor + 1) % self._size
                    s = self._slots[self._cursor]
                    if s is not None and self.usable(s):
                        candidates.append(s)
                if candidates:
                    s = min(candidates, key=lambda slot: slot.pending)
                    self._cursor = s.idx
                    if reserve:
                        s.pending += 1
                    s.hits += 1
                    return s
                self._refreshed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PoolUnavailableError("No validated scraping session is available")
            try:
                await asyncio.wait_for(self._refreshed.wait(), timeout=remaining)
            except TimeoutError as exc:
                raise PoolUnavailableError("No validated scraping session became available within the wait limit") from exc
        raise PoolUnavailableError("Scraping session pool is shutting down")

    @asynccontextmanager
    async def lease(self):
        slot = await self.acquire(reserve=True)
        try:
            yield slot
        finally:
            slot.pending -= 1

    def mark_dead(self, slot: CookieSlot, *, reason: str = "upstream rejected session") -> None:
        if slot.dead:
            return
        slot.dead = True
        logger.info("pool[%d] unavailable (%s); %d leases — scheduling refresh", slot.idx, reason, slot.hits)
        self._schedule_refresh(slot.idx)

    async def _refresh(self, idx: int) -> None:
        try:
            while not self._closing:
                # A failed validation pauses all refreshes, rather than burning
                # replacement cookies concurrently during an upstream outage.
                async with self._recovery_lock:
                    delay = self._retry_after - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._recovery_active = True
                    self._last_recovery_started_at = time.time()
                    try:
                        await self._mint(idx)
                    except Exception as exc:
                        self._retry_after = time.monotonic() + self._retry_delay_s
                        self._last_error = _failure_reason(exc)
                        logger.warning("pool[%d] refresh rejected (%s); retrying with backoff", idx, type(exc).__name__)
                    else:
                        self._refreshes += 1
                        self._persist_to_disk()
                        self._refreshed.set()
                        self._schedule_missing()
                        logger.info("pool[%d] validated and refreshed", idx)
                        return
                    finally:
                        self._recovery_active = False
                        self._last_recovery_completed_at = time.time()
        finally:
            self._refreshing_indices.discard(idx)
            self._refreshed.set()

    async def aclose(self) -> None:
        self._closing = True
        self._refreshed.set()
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *[s.client.aclose() for s in self._slots if s is not None],
            return_exceptions=True,
        )

    @property
    def stats(self) -> dict:
        live_slots = [s for s in self._slots if s and self.usable(s)]
        return {
            "size": self._size,
            "live": len(live_slots),
            "mints": self._mints,
            "refreshes": self._refreshes,
            "rehydrated": self._rehydrated,
            "refreshing": len(self._refreshing_indices),
            "validation_failures": self._validation_failures,
            "last_error": self._last_error,
            "recovery_active": self._recovery_active,
            "last_recovery_started_at": self._last_recovery_started_at,
            "last_recovery_completed_at": self._last_recovery_completed_at,
            "oldest_session_age_s": round(max(time.time() - s.minted_at for s in live_slots), 1) if live_slots else None,
            "last_success_at": max((s.last_success_at or 0 for s in live_slots), default=None),
            "session_strategy": "http" if self._prefer_http_sessions else "browser",
        }


async def _close_later(client: httpx.AsyncClient) -> None:
    try:
        await asyncio.sleep(60)
    finally:
        await client.aclose()

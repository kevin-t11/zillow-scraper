"""
FastAPI server bootstrap.

    python -m px_bypass.serve

Starts the API on $API_PORT (default 8000) after validating the first Evomi
HTTP session. The pool fills in the background and replaces rejected or
expired sessions. Browser setup, when used, runs in isolated workers.

Routes live in `routes.py`. This module only owns process bootstrap.

Environment:
    API_HOST              0.0.0.0
    API_PORT              8000
    POOL_SIZE             24      (cookies in rotation)
    POOL_PER_SLOT_C       5       (concurrent reqs per cookie)
    SEARCH_POOL_SIZE      4       (independently validated discovery sessions)
    HEADLESS              true    (set "false" on a desktop for debugging)
    EVOMI_USERNAME        required
    EVOMI_PASSWORD        required
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from .cookie_pool import CookiePool
from .session_worker import IsolatedMinter
from .routes import router
from .search import SearchCookiePool

logger = logging.getLogger("px_bypass.serve")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    pool_size = int(os.getenv("POOL_SIZE", "24"))
    per_slot_c = int(os.getenv("POOL_PER_SLOT_C", "5"))
    headless = os.getenv("HEADLESS", "true").lower() != "false"

    logger.info("preparing isolated browser workers (headless=%s)…", headless)
    minter = IsolatedMinter(headless=headless, timeout_s=float(os.getenv("MINT_TIMEOUT_S", "60")))
    logger.info("minting cookie pool of %d…", pool_size)
    pool = await CookiePool.create(minter.mint, pool_size, per_slot_concurrency=per_slot_c)
    app.state.minter = minter
    app.state.pool = pool
    try:
        search_pool = await SearchCookiePool.create(
            minter.mint, int(os.getenv("SEARCH_POOL_SIZE", "4")), per_slot_concurrency=per_slot_c,
            persist_path=Path(os.getenv("SEARCH_POOL_PERSIST", str(Path.home() / ".cache/zillow_pool/search-slots.json"))),
        )
        app.state.search_pool = search_pool
        yield
    finally:
        if getattr(app.state, "search_pool", None) is not None:
            await app.state.search_pool.aclose()
        logger.info("shutting down…")
        await pool.aclose()
        await minter.close()


app = FastAPI(title="Zillow PX Bypass", version="2.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=3)
app.include_router(router)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        "px_bypass.serve:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()

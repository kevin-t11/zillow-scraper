#!/usr/bin/env python3
"""
Single entry point for the Zillow PX-Bypass server.

  python run.py                    # http://0.0.0.0:8000, headless minter
  python run.py --port 3000        # custom port
  python run.py --host 127.0.0.1   # localhost only
  python run.py --reload           # auto-reload on code changes
  python run.py --headed           # show the patched Chromium (debugging)
  python run.py --pool-size 12     # larger cookie pool

Boots `px_bypass.serve:app` — which on lifespan-startup launches a single
patched Chromium and mints `--pool-size` cookies in parallel via Evomi
residential. Routes are defined in `px_bypass/routes.py`.
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Zillow PX-Bypass API server")
    parser.add_argument("--host", default=os.getenv("API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--headed", action="store_true", help="show patched Chromium window")
    parser.add_argument("--pool-size", type=int, default=int(os.getenv("POOL_SIZE", "24")))
    parser.add_argument("--per-slot-c", type=int, default=int(os.getenv("POOL_PER_SLOT_C", "5")))
    args = parser.parse_args()

    # Pass tuning knobs to the serve module via env (lifespan reads them there).
    os.environ["POOL_SIZE"] = str(args.pool_size)
    os.environ["POOL_PER_SLOT_C"] = str(args.per_slot_c)
    os.environ["HEADLESS"] = "false" if args.headed else "true"

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Bulk batches otherwise emit thousands of upstream request log lines.
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    print()
    print("  Zillow PX-Bypass API")
    print(f"  http://{args.host}:{args.port}")
    print(f"  pool_size={args.pool_size}  per_slot_c={args.per_slot_c}  "
          f"headless={'no' if args.headed else 'yes'}")
    print()
    print("  Endpoints:")
    print("    GET  /info                  - service metadata + endpoint catalog")
    print("    GET  /health                - cookie pool stats")
    print("    POST /v2/search/listings    - discover listings by market/filter")
    print("    POST /v2/scrape/urls        - scrape Zillow listing URLs directly")
    print("    POST /v2/scrape/addresses   - resolve addresses → scrape")
    print("    GET  /docs                  - Swagger UI")
    print()

    uvicorn.run(
        "px_bypass.serve:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="debug" if args.verbose else "info",
    )


if __name__ == "__main__":
    main()

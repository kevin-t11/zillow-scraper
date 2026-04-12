#!/usr/bin/env python3
"""
run.py — Entry point for the Zillow Scraper

Usage:
  python run.py                          # Start API server (default port 8000)
  python run.py --port 3000              # Custom port
  python run.py --host 127.0.0.1         # Localhost only
"""

import argparse
import logging
import uvicorn





from app.config import load_settings


def main():
    parser = argparse.ArgumentParser(description="Zillow Scraper API Server")
    parser.add_argument("--host", default=None, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\n  Zillow Scraper API")
    print(f"  http://{host}:{port}")
    print(f"  Proxy: {'enabled (' + settings.brightdata_zone + ')' if settings.brightdata_customer_id else 'disabled'}")
    print(f"\n  Endpoints:")
    print(f"    POST /scrape       - Stream results via SSE (batches of {settings.batch_size})")
    print(f"    POST /scrape/sync  - Return all results as JSON")
    print(f"    POST /scrape/urls  - Fast: scrape Zillow URLs directly (skip ZPID lookup)")
    print(f"    GET  /health       - Health check")
    print(f"    GET  /docs         - Swagger UI\n")

    uvicorn.run(
        "app.server:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="debug" if args.verbose else "info",
    )


if __name__ == "__main__":
    main()

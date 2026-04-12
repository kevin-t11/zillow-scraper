"""
server.py — FastAPI server with SSE streaming batch responses

POST /scrape         → Stream results in batches via SSE
POST /scrape/sync    → Wait for all results, return JSON
GET  /health         → Health check
"""

import json
import time
import uuid
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.config import Settings, load_settings
from app.models import ScrapeRequest, UrlScrapeRequest, JobStatus
from app.proxy import ProxyManager
from app.scraper import (
    scrape_addresses_stream,
    scrape_addresses,
    scrape_urls,
    scrape_urls_full,
    scrape_addresses_full,
)

logger = logging.getLogger("server")

app = FastAPI(
    title="Zillow Scraper API",
    description="Ultra-fast Zillow property scraper with streaming batch responses",
    version="2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Loaded on startup
settings: Optional[Settings] = None
proxy_url: Optional[str] = None


@app.on_event("startup")
async def startup():
    global settings, proxy_url
    settings = load_settings()

    if settings.brightdata_customer_id and settings.brightdata_password:
        pm = ProxyManager.from_settings(settings)
        proxy_url = pm.get_proxy_url()
        logger.info(f"Proxy enabled: {settings.brightdata_zone} @ {settings.brightdata_host}:{settings.brightdata_port}")
    else:
        logger.warning("No proxy configured — Zillow will likely block requests")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "proxy": bool(proxy_url),
        "proxy_zone": settings.brightdata_zone if settings else None,
    }


@app.post("/scrape")
async def scrape_stream(req: ScrapeRequest):
    """
    Stream results via Server-Sent Events (SSE).

    Events:
      - event: batch    → data: {batch_number, results: [...], progress: {done, total}}
      - event: done     → data: {summary: {total, success, errors, elapsed}}
      - event: error    → data: {message: "..."}

    Client reads batches as they arrive. Each batch contains up to `batch_size` results.
    """
    if not req.addresses:
        raise HTTPException(400, "No addresses provided")

    addresses = [a.strip() for a in req.addresses if a.strip()]
    if not addresses:
        raise HTTPException(400, "No valid addresses after filtering")

    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] Starting scrape: {len(addresses)} addresses, concurrency={req.concurrency}, batch_size={req.batch_size}")

    async def event_stream():
        t0 = time.time()
        total = len(addresses)
        done = 0
        success = 0
        errors = 0
        batch_num = 0

        try:
            async for batch in scrape_addresses_stream(
                addresses,
                proxy_url=proxy_url,
                concurrency=req.concurrency,
                max_retries=req.retries,
                batch_size=req.batch_size,
            ):
                batch_num += 1
                batch_dicts = []
                for r in batch:
                    done += 1
                    if r.status == "success":
                        success += 1
                    elif r.status in ("error", "parse_error"):
                        errors += 1
                    batch_dicts.append(r.to_dict())

                elapsed = time.time() - t0
                payload = json.dumps({
                    "batch_number": batch_num,
                    "results": batch_dicts,
                    "progress": {
                        "done": done,
                        "total": total,
                        "success": success,
                        "errors": errors,
                        "elapsed": round(elapsed, 1),
                        "rate": round(done / elapsed, 1) if elapsed > 0 else 0,
                    },
                }, default=str)

                yield f"event: batch\ndata: {payload}\n\n"
                logger.info(f"[{job_id}] Batch {batch_num}: {len(batch)} results ({done}/{total})")

            # Final summary
            elapsed = time.time() - t0
            summary = json.dumps({
                "job_id": job_id,
                "total": total,
                "success": success,
                "errors": errors,
                "not_found": total - success - errors,
                "elapsed": round(elapsed, 1),
                "rate": round(total / elapsed, 1) if elapsed > 0 else 0,
            })
            yield f"event: done\ndata: {summary}\n\n"

        except Exception as e:
            logger.error(f"[{job_id}] Stream error: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Job-Id": job_id,
        },
    )


@app.post("/scrape/urls/fast")
async def scrape_by_urls_fast(req: UrlScrapeRequest):
    """
    FAST URL endpoint — core listing data only, NO nearby homes.
    Use when you don't need nearby homes (one fewer request per zipcode).
    """
    if not req.urls:
        raise HTTPException(400, "No URLs provided")

    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "No valid URLs after filtering")

    invalid = [u for u in urls if "zillow.com" not in u]
    if invalid:
        raise HTTPException(400, f"Non-Zillow URLs found: {invalid[:3]}")

    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] URL scrape: {len(urls)} urls, concurrency={req.concurrency}")

    t0 = time.time()

    results = await scrape_urls(
        urls,
        proxy_url=proxy_url,
        concurrency=req.concurrency,
        max_retries=req.retries,
    )

    elapsed = time.time() - t0
    success = sum(1 for r in results if r.status == "success")
    errors = sum(1 for r in results if r.status in ("error", "parse_error"))

    return {
        "results": [r.to_dict() for r in results],
        "summary": {
            "job_id": job_id,
            "total": len(results),
            "success": success,
            "errors": errors,
            "not_found": len(results) - success - errors,
            "elapsed": round(elapsed, 1),
            "rate": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        },
    }


@app.post("/scrape/addresses")
async def scrape_by_addresses(req: ScrapeRequest):
    """
    Address-based endpoint: resolves each address → fetches full listing data
    + nearby homes (from cached zipcode pages).

    Returns ALL property fields including:
      - year_built, price_per_sqft, living_area_sqft (sqft)
      - price, zestimate, beds/baths, lot size
      - photos, agent, mortgage rates, tax rate
      - nearby_homes (up to 20 per listing, cached by zipcode)
      - schools, price_history, tax_history (when in __NEXT_DATA__)
    """
    if not req.addresses:
        raise HTTPException(400, "No addresses provided")

    addresses = [a.strip() for a in req.addresses if a.strip()]
    if not addresses:
        raise HTTPException(400, "No valid addresses after filtering")

    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] Address full scrape: {len(addresses)} addresses, concurrency={req.concurrency}")

    t0 = time.time()
    results = await scrape_addresses_full(
        addresses,
        proxy_url=proxy_url,
        concurrency=req.concurrency,
        max_retries=req.retries,
    )
    elapsed = time.time() - t0

    success = sum(1 for r in results if r.status == "success")
    errors = sum(1 for r in results if r.status in ("error", "parse_error"))
    with_nearby = sum(1 for r in results if r.nearby_homes)
    with_schools = sum(1 for r in results if r.schools)
    with_history = sum(1 for r in results if r.price_history)

    return {
        "results": [r.to_dict() for r in results],
        "summary": {
            "job_id": job_id,
            "total": len(results),
            "success": success,
            "errors": errors,
            "with_nearby_homes": with_nearby,
            "with_schools": with_schools,
            "with_price_history": with_history,
            "elapsed": round(elapsed, 1),
            "rate": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        },
    }


@app.post("/scrape/urls")
async def scrape_by_urls_full(req: UrlScrapeRequest):
    """
    URL endpoint — full data including nearby homes.

    Accepts Zillow listing URLs and returns ALL property fields:
      - Core: price, zestimate, beds/baths, year_built, price_per_sqft, living_area_sqft
      - Photos, agent, mortgage rates, property tax rate
      - nearby_homes (up to 20 per listing, cached by zipcode)
      - schools, price_history, tax_history (when present in __NEXT_DATA__)

    Example:
        POST /scrape/urls
        {
          "urls": ["https://www.zillow.com/homedetails/.../12345_zpid/"],
          "concurrency": 100,
          "retries": 2
        }
    """
    if not req.urls:
        raise HTTPException(400, "No URLs provided")

    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "No valid URLs after filtering")

    invalid = [u for u in urls if "zillow.com" not in u]
    if invalid:
        raise HTTPException(400, f"Non-Zillow URLs found: {invalid[:3]}")

    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] Full URL scrape: {len(urls)} urls, concurrency={req.concurrency}")

    t0 = time.time()
    results = await scrape_urls_full(
        urls,
        proxy_url=proxy_url,
        concurrency=req.concurrency,
        max_retries=req.retries,
    )
    elapsed = time.time() - t0

    success = sum(1 for r in results if r.status == "success")
    errors = sum(1 for r in results if r.status in ("error", "parse_error"))
    with_nearby = sum(1 for r in results if r.nearby_homes)
    with_schools = sum(1 for r in results if r.schools)
    with_history = sum(1 for r in results if r.price_history)

    return {
        "results": [r.to_dict() for r in results],
        "summary": {
            "job_id": job_id,
            "total": len(results),
            "success": success,
            "errors": errors,
            "with_nearby_homes": with_nearby,
            "with_schools": with_schools,
            "with_price_history": with_history,
            "elapsed": round(elapsed, 1),
            "rate": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        },
    }


@app.post("/scrape/sync")
async def scrape_sync(req: ScrapeRequest):
    """
    Non-streaming: wait for all results and return as JSON.
    Best for small batches (< 50 addresses).
    """
    if not req.addresses:
        raise HTTPException(400, "No addresses provided")

    addresses = [a.strip() for a in req.addresses if a.strip()]
    if not addresses:
        raise HTTPException(400, "No valid addresses")

    if len(addresses) > 500:
        raise HTTPException(400, "Use /scrape (streaming) for > 500 addresses")

    t0 = time.time()

    results = await scrape_addresses(
        addresses,
        proxy_url=proxy_url,
        concurrency=req.concurrency,
        max_retries=req.retries,
    )

    elapsed = time.time() - t0
    success = sum(1 for r in results if r.status == "success")
    errors = sum(1 for r in results if r.status in ("error", "parse_error"))

    return {
        "results": [r.to_dict() for r in results],
        "summary": {
            "total": len(results),
            "success": success,
            "errors": errors,
            "not_found": len(results) - success - errors,
            "elapsed": round(elapsed, 1),
            "rate": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        },
    }

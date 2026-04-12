"""
scraper.py — Core Zillow scraping logic (pure async HTTP)

Strategy (2 requests per address):
  1. Zillow autocomplete API → ZPID (free, no proxy)
  2. Zillow detail page → __NEXT_DATA__ JSON (via proxy)
"""

import asyncio
import json
import re
import logging
import random
import urllib.parse
from typing import Optional, AsyncIterator

import httpx

from app.models import PropertyListing

logger = logging.getLogger("scraper")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def _random_headers() -> dict:
    h = dict(BASE_HEADERS)
    h["User-Agent"] = random.choice(USER_AGENTS)
    return h


# ─────────────────────────────────────────────────────────────────────────────
# Address parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_input_address(address: str) -> dict:
    parts = address.strip().split(',')
    primary = parts[0].strip()

    zip_m = re.search(r'\b(\d{5})\b', primary)
    zipcode = zip_m.group(1) if zip_m else ""

    state_m = re.search(r'\b([A-Z]{2})\s+\d{5}\b', primary.upper())
    state = state_m.group(1) if state_m else ""

    street_num_m = re.match(r'^(\d+[A-Za-z]?)\s+', primary.strip())
    street_number = street_num_m.group(1) if street_num_m else ""

    street_part = primary
    if zipcode:
        street_part = street_part[:street_part.rfind(zipcode)].strip()
    if state:
        street_part = re.sub(r'\b' + state + r'\b', '', street_part, flags=re.IGNORECASE).strip()
    if street_number and street_part.upper().startswith(street_number.upper()):
        street_name = street_part[len(street_number):].strip()
    else:
        street_name = street_part

    full_street = f"{street_number} {street_name}".strip() if street_number else street_name

    return {
        "street_number": street_number.upper(),
        "street_name": street_name.strip().upper(),
        "full_street": full_street.strip().upper(),
        "state": state.upper(),
        "zipcode": zipcode,
    }


def addresses_match(input_addr: str, zillow_street: str, zillow_state: str, zillow_zip: str) -> bool:
    parsed = parse_input_address(input_addr)
    inp_num = parsed["street_number"].upper()
    z_street = zillow_street.upper().strip()

    if not z_street.startswith(inp_num):
        return False

    z_street_name = z_street[len(inp_num):].strip().upper()
    inp_words = [w for w in parsed["street_name"].split() if len(w) > 1]
    z_words = [w for w in z_street_name.split() if len(w) > 1]

    if not inp_words or not z_words:
        return False
    if inp_words[0] != z_words[0]:
        return False
    if parsed["state"] and zillow_state and parsed["state"].upper() != zillow_state.upper():
        return False
    if parsed["zipcode"] and zillow_zip and parsed["zipcode"] != zillow_zip:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# ZPID Resolution
# ─────────────────────────────────────────────────────────────────────────────

async def get_zpid_via_autocomplete(client: httpx.AsyncClient, address: str) -> Optional[tuple[str, str]]:
    parsed = parse_input_address(address)
    street = parsed["full_street"]

    queries = [street]
    if parsed["zipcode"]:
        queries.append(f"{street} {parsed['zipcode']}")
    words = street.split()
    if len(words) >= 3:
        short_street = " ".join(words[:3])
        if short_street != street:
            queries.insert(0, short_street)

    url = "https://www.zillowstatic.com/autocomplete/v3/suggestions"

    for query in queries:
        try:
            params = {"q": query, "resultTypes": "allAddress", "resultCount": 5}
            resp = await client.get(url, params=params, headers=_random_headers(), timeout=15)
            if resp.status_code != 200:
                continue

            for r in resp.json().get("results", []):
                meta = r.get("metaData", {})
                zpid = meta.get("zpid")
                if not zpid:
                    continue
                zpid = str(zpid)
                display = r.get("display", "")

                inp_num = parsed["street_number"]
                if inp_num and not display.strip().startswith(inp_num):
                    continue
                if parsed["state"]:
                    state_code = parsed["state"].upper()
                    if f", {state_code} " not in display.upper() and f" {state_code} " not in display.upper():
                        continue
                if parsed["zipcode"] and parsed["zipcode"] not in display:
                    continue

                logger.debug(f"  Autocomplete: zpid={zpid} | {display}")
                return (zpid, f"https://www.zillow.com/homedetails/{zpid}_zpid/")

        except Exception as e:
            logger.debug(f"  Autocomplete failed for '{query}': {e}")

    return None


async def get_zpid_via_zillow_search(client: httpx.AsyncClient, address: str) -> Optional[tuple[str, str]]:
    parsed = parse_input_address(address)
    search_term = f"{parsed['full_street']} {parsed['zipcode']}"
    search_url = f"https://www.zillow.com/homes/{urllib.parse.quote(search_term)}_rb/"

    try:
        resp = await client.get(search_url, headers=_random_headers(), timeout=30, follow_redirects=True)
        if resp.status_code != 200:
            return None

        final_url = str(resp.url)
        zpid_in_url = re.search(r'/(\d+)_zpid', final_url)
        if zpid_in_url:
            return (zpid_in_url.group(1), final_url)

        matches = re.findall(r'/homedetails/[^"]+?/(\d+)_zpid/', resp.text)
        if matches:
            zpid = matches[0]
            return (zpid, f"https://www.zillow.com/homedetails/{zpid}_zpid/")

    except Exception as e:
        logger.debug(f"  Zillow search failed: {e}")
    return None


async def get_zpid_via_google(client: httpx.AsyncClient, address: str) -> Optional[tuple[str, str]]:
    parsed = parse_input_address(address)
    q = f'{parsed["full_street"]} {parsed["state"]} {parsed["zipcode"]} site:zillow.com'
    google_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(q)}&num=5"

    try:
        headers = _random_headers()
        headers["Referer"] = "https://www.google.com/"
        resp = await client.get(google_url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return None

        pattern = r'https?://www\.zillow\.com/homedetails/[^"&\s<>]+/(\d+)_zpid'
        for m in re.finditer(pattern, resp.text):
            zpid = m.group(1)
            url = re.sub(r'["\s].*$', '', m.group(0))
            if not url.endswith('/'):
                url += '/'
            return (zpid, url)

    except Exception as e:
        logger.debug(f"  Google search failed: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Property Detail Parsing
# ─────────────────────────────────────────────────────────────────────────────

def extract_next_data(html: str) -> Optional[dict]:
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def _is_blocked(html: str) -> bool:
    signals = ["captcha", "cf-browser-verification", "datadome", "px-captcha", "are you a human"]
    lower = html.lower()
    return any(s in lower for s in signals) and len(html) < 80_000


def _extract_gdp_property(data: dict) -> dict:
    """Walk __NEXT_DATA__ to find the property dict in gdpClientCache."""
    props = data.get("props", {})
    page_props = props.get("pageProps", {})

    # Try pageProps.gdpClientCache first
    for source in [
        page_props.get("gdpClientCache", {}),
        page_props.get("componentProps", {}).get("gdpClientCache", {}),
    ]:
        gdp = source
        if isinstance(gdp, str):
            try:
                gdp = json.loads(gdp)
            except Exception:
                continue
        if isinstance(gdp, dict):
            for val in gdp.values():
                if isinstance(val, dict) and "property" in val:
                    return val["property"]
    return {}


def _extract_best_photo_url(photo: dict) -> str:
    """Get the highest-resolution photo URL from a Zillow photo object."""
    # Direct URL
    url = photo.get("url", "")
    if url:
        return url

    # mixedSources → pick largest jpeg
    sources = photo.get("mixedSources", {})
    if isinstance(sources, dict):
        jpegs = sources.get("jpeg", []) or []
        if jpegs:
            # Sort by width desc, pick largest
            best = max(jpegs, key=lambda j: j.get("width", 0))
            return best.get("url", "")
    return ""


def parse_property_from_next_data(data: dict, input_address: str) -> PropertyListing:
    p = PropertyListing(input_address=input_address, status="success")
    try:
        prop_data = _extract_gdp_property(data)

        if not prop_data:
            p.status = "not_found"
            return p

        p.raw_json = prop_data
        reso = prop_data.get("resoFacts", {}) or {}

        # ── Identity ──────────────────────────────────────────────────────
        p.zpid = str(prop_data.get("zpid", ""))
        addr = prop_data.get("address", {}) or {}
        p.street = addr.get("streetAddress", "") or prop_data.get("streetAddress", "")
        p.city = addr.get("city", "") or prop_data.get("city", "")
        p.state = addr.get("state", "") or prop_data.get("state", "")
        p.zipcode = addr.get("zipcode", "") or prop_data.get("zipcode", "")
        p.full_address = f"{p.street}, {p.city}, {p.state} {p.zipcode}".strip(", ")

        raw_url = prop_data.get("hdpUrl", "") or prop_data.get("url", "")
        p.url = ("https://www.zillow.com" + raw_url) if raw_url and not raw_url.startswith("http") else raw_url
        if not p.url and p.zpid:
            p.url = f"https://www.zillow.com/homedetails/{p.zpid}_zpid/"

        # ── Price ─────────────────────────────────────────────────────────
        p.price = prop_data.get("price") or prop_data.get("unformattedPrice")
        p.price_str = prop_data.get("priceStr", "") or (f"${p.price:,.0f}" if p.price else "")
        p.zestimate = prop_data.get("zestimate")
        p.rent_zestimate = prop_data.get("rentZestimate")
        p.last_sold_price = prop_data.get("lastSoldPrice")
        p.price_per_sqft = reso.get("pricePerSquareFoot") or prop_data.get("pricePerSquareFoot")

        # ── Property facts ────────────────────────────────────────────────
        p.bedrooms = prop_data.get("bedrooms") or reso.get("bedrooms")
        p.bathrooms = prop_data.get("bathrooms") or reso.get("bathrooms")
        p.living_area_sqft = prop_data.get("livingArea") or prop_data.get("livingAreaValue")
        p.lot_size_sqft = prop_data.get("lotSize") or prop_data.get("lotAreaValue")
        p.home_type = prop_data.get("homeType", "") or reso.get("homeType", "")
        p.year_built = reso.get("yearBuilt") or prop_data.get("yearBuilt")
        p.listing_status = prop_data.get("homeStatus", "") or prop_data.get("listingStatus", "")

        # Parking — from resoFacts
        p.parking_spaces = reso.get("parkingCapacity")
        p.garage_spaces = reso.get("garageParkingCapacity") or reso.get("garageSpaces")
        p.parking_features = reso.get("parkingFeatures", []) or []

        # HVAC — from resoFacts (may not be in initial page load)
        p.has_cooling = reso.get("hasCooling") or prop_data.get("hasCooling")
        p.has_heating = reso.get("hasHeating") or prop_data.get("hasHeating")
        p.heating_type = ", ".join(reso.get("heating", []) or [])
        p.cooling_type = ", ".join(reso.get("cooling", []) or [])

        # ── Listing metadata ──────────────────────────────────────────────
        p.days_on_zillow = prop_data.get("daysOnZillow")
        if p.days_on_zillow is None:
            toz = prop_data.get("timeOnZillow", "")
            if toz:
                p.days_on_zillow = toz  # keep string like "2 days" / "12 hours"
        p.views = prop_data.get("pageViewCount")
        p.saves = prop_data.get("favoriteCount")
        p.monthly_hoa_fee = prop_data.get("monthlyHoaFee")

        # Dates — datePosted is a timestamp (ms), dateSoldString is already a string
        date_posted_ts = prop_data.get("datePosted")
        if date_posted_ts and isinstance(date_posted_ts, (int, float)):
            from datetime import datetime, timezone
            p.listing_date = datetime.fromtimestamp(date_posted_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            p.listing_date = prop_data.get("datePostedString", "") or prop_data.get("listingDate", "")
        p.sold_date = prop_data.get("dateSoldString", "") or prop_data.get("dateSold", "")
        p.last_sold_date = p.sold_date

        # ── Agent / broker ────────────────────────────────────────────────
        attr = prop_data.get("attributionInfo", {}) or {}
        p.agent_name = attr.get("agentName", "") or attr.get("listingAgentName", "")
        p.agent_phone = attr.get("agentPhoneNumber", "") or attr.get("listingAgentPhoneNumber", "")
        p.agent_email = attr.get("agentEmail", "")
        p.broker_name = attr.get("brokerName", "") or attr.get("buyerBrokerageName", "")
        p.listing_office = attr.get("mlsName", "") or attr.get("listingOfficeName", "")
        p.mls_id = attr.get("mlsId", "") or prop_data.get("mlsid", "")

        # ── Tax & financial ───────────────────────────────────────────────
        p.property_tax_rate = prop_data.get("propertyTaxRate")

        tax_hist = prop_data.get("taxHistory", []) or []
        if tax_hist:
            p.annual_tax = tax_hist[0].get("taxPaid")
            p.tax_year = tax_hist[0].get("time")
            p.tax_history = [
                {"year": t.get("time"), "tax_paid": t.get("taxPaid"), "assessed_value": t.get("value")}
                for t in tax_hist
            ]

        # Mortgage rates
        rates = prop_data.get("mortgageZHLRates", {}) or {}
        thirty = rates.get("thirtyYearFixedBucket", {}) or {}
        fifteen = rates.get("fifteenYearFixedBucket", {}) or {}
        arm5 = rates.get("arm5Bucket", {}) or {}
        p.mortgage_rate_30yr = thirty.get("rate")
        p.mortgage_rate_15yr = fifteen.get("rate")
        p.mortgage_rate_arm5 = arm5.get("rate")

        # ── Description & features ────────────────────────────────────────
        p.description = prop_data.get("description", "")

        # Highlights from homeInsights
        insights_list = prop_data.get("homeInsights", []) or []
        if insights_list:
            first_group = insights_list[0] if isinstance(insights_list[0], dict) else {}
            for insight in (first_group.get("insights", []) or []):
                phrases = insight.get("phrases", [])
                if phrases:
                    p.highlights = list(phrases)
                    break

        p.appliances = reso.get("appliances", []) or []
        p.interior_features = reso.get("interiorFeatures", []) or []
        p.exterior_features = reso.get("exteriorFeatures", []) or []
        p.community_features = reso.get("communityFeatures", []) or []

        # ── Location ──────────────────────────────────────────────────────
        p.latitude = prop_data.get("latitude")
        p.longitude = prop_data.get("longitude")
        nbr = prop_data.get("neighborhoodRegion", {})
        p.neighborhood = nbr.get("name", "") if isinstance(nbr, dict) else ""
        parent = prop_data.get("parentRegion", {})
        if isinstance(parent, dict):
            p.county = parent.get("name", "") or prop_data.get("county", "")
        else:
            p.county = prop_data.get("county", "")

        # ── Media ─────────────────────────────────────────────────────────
        p.photo_count = prop_data.get("photoCount", 0) or 0

        # Zillow now uses responsivePhotos / originalPhotos instead of photos
        raw_photos = (
            prop_data.get("responsivePhotos", [])
            or prop_data.get("originalPhotos", [])
            or prop_data.get("photos", [])
            or prop_data.get("images", [])
            or []
        )
        for ph in raw_photos[:50]:
            if isinstance(ph, dict):
                url = _extract_best_photo_url(ph)
            elif isinstance(ph, str):
                url = ph
            else:
                continue
            if url:
                p.photos.append(url)

        # Virtual tour
        vt = prop_data.get("thirdPartyVirtualTour", {}) or {}
        p.virtual_tour_url = vt.get("externalUrl", "") or vt.get("lightboxUrl", "") or prop_data.get("virtualTourUrl", "") or ""

        # ── Schools ───────────────────────────────────────────────────────
        for s in (prop_data.get("schools", []) or []):
            if isinstance(s, dict):
                p.schools.append({
                    "name": s.get("name", ""),
                    "rating": s.get("rating"),
                    "type": s.get("type", ""),
                    "grades": s.get("grades", ""),
                    "distance": s.get("distance"),
                })

        # ── Price history ─────────────────────────────────────────────────
        for ev in (prop_data.get("priceHistory", []) or []):
            if isinstance(ev, dict):
                p.price_history.append({
                    "date": ev.get("date", ""),
                    "price": ev.get("price"),
                    "event": ev.get("event", ""),
                    "source": ev.get("source", ""),
                    "price_change_rate": ev.get("priceChangeRate"),
                })

    except Exception as e:
        p.status = "parse_error"
        p.error_msg = str(e)
        logger.debug(f"Parse error for {input_address}: {e}", exc_info=True)

    return p


# ─────────────────────────────────────────────────────────────────────────────
# Core: Scrape one address
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_one(
    proxy_client: httpx.AsyncClient,
    direct_client: httpx.AsyncClient,
    address: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
) -> PropertyListing:
    async with semaphore:
        saw_zpid = False

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await asyncio.sleep(attempt * 1.5 + random.uniform(0.3, 1.0))

                logger.debug(f"[{attempt}] Resolving: {address}")

                # Step 1: Find ZPID (free, no proxy)
                zpid_result = await get_zpid_via_autocomplete(direct_client, address)

                if not zpid_result:
                    zpid_result = await get_zpid_via_zillow_search(proxy_client, address)

                if not zpid_result:
                    zpid_result = await get_zpid_via_google(direct_client, address)

                if not zpid_result:
                    if attempt < max_retries:
                        continue
                    return PropertyListing(
                        input_address=address,
                        status="not_found",
                        error_msg="Address not found via autocomplete/Zillow/Google",
                        attempts=attempt,
                    )

                zpid, detail_url = zpid_result
                saw_zpid = True

                # Step 2: Fetch detail page (via proxy)
                if not detail_url.startswith('http'):
                    detail_url = f"https://www.zillow.com/homedetails/{zpid}_zpid/"

                headers = _random_headers()
                headers["Referer"] = "https://www.zillow.com/"
                resp = await proxy_client.get(
                    detail_url, headers=headers, timeout=30, follow_redirects=True
                )

                if resp.status_code != 200:
                    if attempt < max_retries:
                        continue
                    return PropertyListing(
                        input_address=address, status="error",
                        error_msg=f"HTTP {resp.status_code}", zpid=zpid,
                        url=detail_url, attempts=attempt,
                    )

                html = resp.text

                if _is_blocked(html):
                    if attempt < max_retries:
                        continue
                    return PropertyListing(
                        input_address=address, status="error",
                        error_msg="Blocked by anti-bot", zpid=zpid,
                        url=detail_url, attempts=attempt,
                    )

                # Step 3: Parse __NEXT_DATA__
                next_data = extract_next_data(html)
                if next_data:
                    result = parse_property_from_next_data(next_data, address)
                    result.zpid = result.zpid or zpid
                    result.url = result.url or str(resp.url)
                    result.attempts = attempt

                    if result.status == "success" and result.street:
                        if not addresses_match(address, result.street, result.state, result.zipcode):
                            return PropertyListing(
                                input_address=address, status="not_found",
                                error_msg=f"Address mismatch: got '{result.full_address}'",
                                attempts=attempt,
                            )
                        return result

                    if result.status in ("success", "partial"):
                        return result

                return PropertyListing(
                    input_address=address, status="partial",
                    zpid=zpid, url=str(resp.url),
                    error_msg="No __NEXT_DATA__ found", attempts=attempt,
                )

            except httpx.TimeoutException:
                logger.debug(f"  Timeout attempt {attempt}: {address}")
            except Exception as e:
                logger.debug(f"  Error attempt {attempt}: {e}", exc_info=True)

        return PropertyListing(
            input_address=address,
            status="error" if saw_zpid else "not_found",
            error_msg="All retries exhausted",
            attempts=max_retries,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fast: Scrape one URL directly (skip ZPID resolution)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_one_url(
    proxy_client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = 2,
) -> PropertyListing:
    """Fetch a Zillow detail page URL directly and parse it. Single HTTP request."""
    async with semaphore:
        zpid_match = re.search(r'/(\d+)_zpid', url)
        zpid = zpid_match.group(1) if zpid_match else ""

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await asyncio.sleep(attempt * 0.5 + random.uniform(0.1, 0.5))

                headers = _random_headers()
                headers["Referer"] = "https://www.zillow.com/"
                resp = await proxy_client.get(
                    url, headers=headers, timeout=30, follow_redirects=True
                )

                if resp.status_code != 200:
                    if attempt < max_retries:
                        continue
                    return PropertyListing(
                        input_address=url, status="error",
                        error_msg=f"HTTP {resp.status_code}", zpid=zpid,
                        url=url, attempts=attempt,
                    )

                html = resp.text

                if _is_blocked(html):
                    if attempt < max_retries:
                        continue
                    return PropertyListing(
                        input_address=url, status="error",
                        error_msg="Blocked by anti-bot", zpid=zpid,
                        url=url, attempts=attempt,
                    )

                next_data = extract_next_data(html)
                if next_data:
                    result = parse_property_from_next_data(next_data, url)
                    result.zpid = result.zpid or zpid
                    result.url = result.url or str(resp.url)
                    result.attempts = attempt
                    return result

                return PropertyListing(
                    input_address=url, status="partial",
                    zpid=zpid, url=str(resp.url),
                    error_msg="No __NEXT_DATA__ found", attempts=attempt,
                )

            except httpx.TimeoutException:
                logger.debug(f"  Timeout attempt {attempt}: {url}")
            except Exception as e:
                logger.debug(f"  Error attempt {attempt}: {e}", exc_info=True)

        return PropertyListing(
            input_address=url, status="error",
            error_msg="All retries exhausted", zpid=zpid,
            url=url, attempts=max_retries,
        )


async def fetch_nearby_homes_by_zip(
    proxy_client: httpx.AsyncClient,
    city_state_zip: tuple[str, str, str],
    semaphore: asyncio.Semaphore,
    max_retries: int = 2,
) -> list[dict]:
    """
    Fetch the Zillow zipcode search page and return list of nearby homes.
    city_state_zip: (city, state, zipcode) e.g. ('Downey', 'CA', '90240')
    """
    city, state, zipcode = city_state_zip
    if not zipcode:
        return []

    # Universal Zillow zipcode URL — works for all zips including LA neighborhoods
    url = f"https://www.zillow.com/{zipcode}/"

    async with semaphore:
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await asyncio.sleep(0.5 * attempt)

                headers = _random_headers()
                headers["Referer"] = "https://www.zillow.com/"
                resp = await proxy_client.get(url, headers=headers, timeout=30, follow_redirects=True)

                if resp.status_code != 200:
                    if attempt < max_retries:
                        continue
                    return []

                next_data = extract_next_data(resp.text)
                if not next_data:
                    return []

                sps = next_data.get("props", {}).get("pageProps", {}).get("searchPageState", {})
                cat1 = sps.get("cat1", {}) or {}
                results = (cat1.get("searchResults", {}) or {}).get("listResults", []) or []

                homes = []
                for r in results[:30]:  # cap at 30
                    addr = r.get("address", "") or ""
                    homes.append({
                        "zpid": str(r.get("zpid", "")),
                        "price": r.get("unformattedPrice") or r.get("price"),
                        "price_str": r.get("price", ""),
                        "bedrooms": r.get("beds"),
                        "bathrooms": r.get("baths"),
                        "living_area": r.get("area"),
                        "address": addr,
                        "street": r.get("addressStreet", ""),
                        "city": r.get("addressCity", ""),
                        "state": r.get("addressState", ""),
                        "zipcode": r.get("addressZipcode", ""),
                        "url": r.get("detailUrl", ""),
                        "image": r.get("imgSrc", ""),
                        "status": r.get("statusText", ""),
                        "latitude": (r.get("latLong") or {}).get("latitude") if isinstance(r.get("latLong"), dict) else None,
                        "longitude": (r.get("latLong") or {}).get("longitude") if isinstance(r.get("latLong"), dict) else None,
                    })
                return homes

            except Exception as e:
                logger.debug(f"  Nearby fetch error attempt {attempt} for {zipcode}: {e}")

        return []


async def _scrape_url_with_nearby(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
    max_retries: int,
    nearby_cache: dict,
    nearby_locks: dict,
) -> PropertyListing:
    """Scrape one URL, then immediately fetch its zipcode (if not cached)."""
    result = await scrape_one_url(client, url, sem, max_retries)
    if result.status == "success" and result.zipcode:
        csz = (result.city, result.state, result.zipcode)
        # Use a per-zip lock so duplicate zipcodes only fetch once
        if csz not in nearby_locks:
            nearby_locks[csz] = asyncio.Lock()
        async with nearby_locks[csz]:
            if csz not in nearby_cache:
                nearby_cache[csz] = await fetch_nearby_homes_by_zip(
                    client, csz, sem, max_retries
                )
        nearby = nearby_cache.get(csz, [])
        result.nearby_homes = [h for h in nearby if h.get("zpid") != result.zpid][:20]
    return result


async def _scrape_addr_with_nearby(
    proxy_client: httpx.AsyncClient,
    direct_client: httpx.AsyncClient,
    addr: str,
    sem: asyncio.Semaphore,
    max_retries: int,
    nearby_cache: dict,
    nearby_locks: dict,
) -> PropertyListing:
    """Scrape one address, then immediately fetch its zipcode (if not cached)."""
    result = await scrape_one(proxy_client, direct_client, addr, sem, max_retries)
    if result.status == "success" and result.zipcode:
        csz = (result.city, result.state, result.zipcode)
        if csz not in nearby_locks:
            nearby_locks[csz] = asyncio.Lock()
        async with nearby_locks[csz]:
            if csz not in nearby_cache:
                nearby_cache[csz] = await fetch_nearby_homes_by_zip(
                    proxy_client, csz, sem, max_retries
                )
        nearby = nearby_cache.get(csz, [])
        result.nearby_homes = [h for h in nearby if h.get("zpid") != result.zpid][:20]
    return result


async def scrape_urls_full(
    urls: list[str],
    proxy_url: Optional[str] = None,
    concurrency: int = 50,
    max_retries: int = 2,
) -> list[PropertyListing]:
    """
    URL-based full scraper: fetches listing pages + zipcode pages for nearby homes.
    Caches zipcode pages so each unique zip is only fetched once.
    """
    logger.info(f"URL full scrape: {len(urls)} urls, concurrency={concurrency}")

    limits = httpx.Limits(
        max_connections=concurrency + 20,
        max_keepalive_connections=concurrency,
        keepalive_expiry=30,
    )

    async with httpx.AsyncClient(
        proxy=proxy_url,
        limits=limits,
        follow_redirects=True,
        verify=not bool(proxy_url),
        http2=True,
        timeout=httpx.Timeout(35, connect=15),
    ) as client:
        sem = asyncio.Semaphore(concurrency)
        nearby_cache: dict = {}
        nearby_locks: dict = {}
        tasks = [
            asyncio.ensure_future(
                _scrape_url_with_nearby(
                    client, u.strip(), sem, max_retries, nearby_cache, nearby_locks
                )
            )
            for u in urls if u.strip()
        ]
        return list(await asyncio.gather(*tasks))


async def scrape_addresses_full(
    addresses: list[str],
    proxy_url: Optional[str] = None,
    concurrency: int = 30,
    max_retries: int = 3,
) -> list[PropertyListing]:
    """
    Address-based full scraper: resolves addresses → fetches listing pages
    → fetches nearby homes by zipcode (cached).
    """
    logger.info(f"Address full scrape: {len(addresses)} addresses, concurrency={concurrency}")

    limits = httpx.Limits(
        max_connections=concurrency + 20,
        max_keepalive_connections=concurrency,
        keepalive_expiry=30,
    )

    proxy_client_ctx = httpx.AsyncClient(
        proxy=proxy_url,
        limits=limits,
        follow_redirects=True,
        verify=not bool(proxy_url),
        http2=True,
        timeout=httpx.Timeout(45, connect=20),
    )
    direct_client_ctx = httpx.AsyncClient(
        limits=limits,
        follow_redirects=True,
        verify=True,
        http2=True,
        timeout=httpx.Timeout(15, connect=10),
    )

    async with proxy_client_ctx as proxy_client, direct_client_ctx as direct_client:
        sem = asyncio.Semaphore(concurrency)
        nearby_cache: dict = {}
        nearby_locks: dict = {}
        tasks = [
            asyncio.ensure_future(
                _scrape_addr_with_nearby(
                    proxy_client, direct_client, addr.strip(), sem,
                    max_retries, nearby_cache, nearby_locks,
                )
            )
            for addr in addresses if addr.strip()
        ]
        return list(await asyncio.gather(*tasks))


async def scrape_urls(
    urls: list[str],
    proxy_url: Optional[str] = None,
    concurrency: int = 50,
    max_retries: int = 2,
) -> list[PropertyListing]:
    """Scrape a list of Zillow URLs directly with high concurrency."""
    logger.info(f"URL scrape: {len(urls)} urls, concurrency={concurrency}")

    limits = httpx.Limits(
        max_connections=concurrency + 10,
        max_keepalive_connections=concurrency,
        keepalive_expiry=30,
    )

    async with httpx.AsyncClient(
        proxy=proxy_url,
        limits=limits,
        follow_redirects=True,
        verify=not bool(proxy_url),
        http2=True,
        timeout=httpx.Timeout(35, connect=15),
    ) as client:
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [
            asyncio.ensure_future(
                scrape_one_url(client, url.strip(), semaphore, max_retries)
            )
            for url in urls if url.strip()
        ]
        return list(await asyncio.gather(*tasks))


# ─────────────────────────────────────────────────────────────────────────────
# Streaming batch orchestrator (yields results as they complete)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_addresses_stream(
    addresses: list[str],
    proxy_url: Optional[str] = None,
    concurrency: int = 30,
    max_retries: int = 3,
    batch_size: int = 100,
) -> AsyncIterator[list[PropertyListing]]:
    """
    Yields batches of PropertyListing as they complete.
    Every `batch_size` results (or final remainder), yields a batch.
    """
    total = len(addresses)
    logger.info(f"Starting: {total} addresses, concurrency={concurrency}, batch_size={batch_size}")

    limits = httpx.Limits(
        max_connections=concurrency + 10,
        max_keepalive_connections=concurrency,
        keepalive_expiry=30,
    )

    proxy_client_ctx = httpx.AsyncClient(
        proxy=proxy_url,
        limits=limits,
        follow_redirects=True,
        verify=not bool(proxy_url),
        http2=True,
        timeout=httpx.Timeout(45, connect=20),
    )

    direct_client_ctx = httpx.AsyncClient(
        limits=limits,
        follow_redirects=True,
        verify=True,
        http2=True,
        timeout=httpx.Timeout(15, connect=10),
    )

    async with proxy_client_ctx as proxy_client, direct_client_ctx as direct_client:
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [
            asyncio.ensure_future(
                scrape_one(proxy_client, direct_client, addr, semaphore, max_retries)
            )
            for addr in addresses
        ]

        batch: list[PropertyListing] = []
        done_count = 0

        for coro in asyncio.as_completed(tasks):
            result = await coro
            batch.append(result)
            done_count += 1

            if len(batch) >= batch_size or done_count == total:
                logger.info(f"Yielding batch: {len(batch)} results ({done_count}/{total} done)")
                yield list(batch)
                batch.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming version (for CLI / simple use)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_addresses(
    addresses: list[str],
    proxy_url: Optional[str] = None,
    concurrency: int = 30,
    max_retries: int = 3,
    progress_callback=None,
) -> list[PropertyListing]:
    all_results: list[PropertyListing] = []
    total = len(addresses)
    done = 0

    async for batch in scrape_addresses_stream(
        addresses, proxy_url=proxy_url, concurrency=concurrency,
        max_retries=max_retries, batch_size=total,  # single batch
    ):
        for result in batch:
            all_results.append(result)
            done += 1
            if progress_callback:
                progress_callback(done, total, result)

    return all_results

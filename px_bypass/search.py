"""Zillow search/discovery helpers for batch listing collection.

This module discovers listing URLs from Zillow search/map results, then the
route layer can pass those URLs into the existing detail scraper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Optional

import httpx

from .cookie_pool import CookiePool

logger = logging.getLogger("px_bypass.search")

AUTOCOMPLETE_ENDPOINT = "https://www.zillowstatic.com/autocomplete/v3/suggestions"
ZILLOW_SEARCH_ENDPOINT = "https://www.zillow.com/async-create-search-page-state"

ZILLOW_HOME_TYPE_KEYS = [
    "isSingleFamily",
    "isMultiFamily",
    "isTownhouse",
    "isCondo",
    "isLotLand",
    "isApartment",
    "isManufactured",
    "isApartmentOrCondo",
]

HOME_TYPE_TO_ZILLOW_KEY = {
    "single_family": "isSingleFamily",
    "multi_family": "isMultiFamily",
    "townhouse": "isTownhouse",
    "condo": "isCondo",
    "land": "isLotLand",
    "apartment": "isApartment",
    "manufactured": "isManufactured",
    "apartment_or_condo": "isApartmentOrCondo",
}

# Zillow searchQueryState regionSelection numeric types used by the web app.
REGION_TYPE_TO_CODE = {
    "state": 2,
    "county": 4,
    "city": 6,
    "zipcode": 7,
    "neighborhood": 8,
}

SEARCH_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://www.zillow.com",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class SearchError(RuntimeError):
    """Raised when Zillow search discovery cannot complete."""


@dataclass
class SearchLocation:
    query: str
    display: str
    region_id: Optional[int]
    region_type: str
    city: str = ""
    county: str = ""
    state: str = ""
    zipcode: str = ""
    country: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @property
    def region_selection(self) -> list[dict[str, int]]:
        code = REGION_TYPE_TO_CODE.get((self.region_type or "").lower())
        if self.region_id is None or code is None:
            return []
        return [{"regionId": self.region_id, "regionType": code}]


@dataclass
class SearchListingPreview:
    zpid: str
    url: str
    full_address: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""
    price: Optional[float] = None
    price_str: str = ""
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    living_area_sqft: Optional[float] = None
    lot_size_sqft: Optional[float] = None
    home_type: str = ""
    listing_status: str = ""
    days_on_zillow: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image: str = ""
    description: str = ""
    source: str = "zillow-search"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchStats:
    discovered: int = 0
    discarded_out_of_location: int = 0
    total_result_count: Optional[int] = None
    search_requests: int = 0
    blocked: int = 0
    elapsed_s: float = 0.0
    tiled: bool = False


def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace("$", "").replace(",", "").replace(" ", "")
        if not normalized:
            return None
        suffix = normalized[-1:].lower()
        base = normalized[:-1] if suffix in {"k", "m", "b"} else normalized
        try:
            parsed = float(base)
        except ValueError:
            return None
        if suffix == "k":
            return parsed * 1_000
        if suffix == "m":
            return parsed * 1_000_000
        if suffix == "b":
            return parsed * 1_000_000_000
        return parsed
    return None


def _to_int(value: Any) -> Optional[int]:
    n = _to_number(value)
    return int(n) if n is not None else None


def _to_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _to_id_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return str(int(value))
    return ""


def _nested(obj: Any, *path: str) -> Any:
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def bounds_for_radius(latitude: float, longitude: float, radius_miles: float) -> dict[str, float]:
    radius_km = radius_miles * 1.60934
    lat_delta = radius_km / 111.32
    lng_delta = radius_km / (111.32 * math.cos(math.radians(latitude)))
    return {
        "north": latitude + lat_delta,
        "south": latitude - lat_delta,
        "east": longitude + lng_delta,
        "west": longitude - lng_delta,
    }


def split_bounds(bounds: dict[str, float]) -> list[dict[str, float]]:
    mid_lat = (bounds["north"] + bounds["south"]) / 2
    mid_lng = (bounds["east"] + bounds["west"]) / 2
    return [
        {"north": bounds["north"], "south": mid_lat, "east": mid_lng, "west": bounds["west"]},
        {"north": bounds["north"], "south": mid_lat, "east": bounds["east"], "west": mid_lng},
        {"north": mid_lat, "south": bounds["south"], "east": mid_lng, "west": bounds["west"]},
        {"north": mid_lat, "south": bounds["south"], "east": bounds["east"], "west": mid_lng},
    ]


def build_home_type_filter(home_types: Iterable[str]) -> dict[str, dict[str, bool]]:
    active = {
        HOME_TYPE_TO_ZILLOW_KEY[t]
        for t in home_types
        if t in HOME_TYPE_TO_ZILLOW_KEY
    }
    if not active:
        active = {"isMultiFamily"}

    if "isCondo" in active:
        active.add("isApartmentOrCondo")

    filters: dict[str, dict[str, bool]] = {}
    for key in ZILLOW_HOME_TYPE_KEYS:
        if key not in active:
            filters[key] = {"value": False}
    return filters


def build_filter_state(
    *,
    status: str,
    home_types: Iterable[str],
    include_fsbo: bool = True,
    include_new_construction: bool = True,
    include_coming_soon: bool = True,
    include_foreclosures: bool = False,
    include_auctions: bool = False,
) -> dict[str, Any]:
    status = status.lower()
    if status == "for_rent":
        return {
            "isForRent": {"value": True},
            "isForSaleByAgent": {"value": False},
            "isForSaleByOwner": {"value": False},
            "isNewConstruction": {"value": False},
            "isComingSoon": {"value": False},
            "isAuction": {"value": False},
            "isForSaleForeclosure": {"value": False},
            **build_home_type_filter(home_types),
        }

    if status == "sold":
        return {
            "isRecentlySold": {"value": True},
            "sortSelection": {"value": "globalrelevanceex"},
            **build_home_type_filter(home_types),
        }

    if status != "for_sale":
        raise ValueError(f"unsupported search status: {status}")

    return {
        "isForSaleByAgent": {"value": True},
        "isForSaleByOwner": {"value": include_fsbo},
        "isNewConstruction": {"value": include_new_construction},
        "isComingSoon": {"value": include_coming_soon},
        "isAuction": {"value": include_auctions},
        "isForSaleForeclosure": {"value": include_foreclosures},
        "isRecentlySold": {"value": False},
        "sortSelection": {"value": "newest"},
        **build_home_type_filter(home_types),
    }


def _search_referer(location: SearchLocation, home_types: Iterable[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", location.display.lower()).strip("-")
    if "multi_family" in set(home_types):
        return f"https://www.zillow.com/{slug}/multi-family-home_att/"
    return f"https://www.zillow.com/{slug}_rb/"


def build_search_payload(
    *,
    location: SearchLocation,
    bounds: dict[str, float],
    status: str,
    home_types: list[str],
    page: int = 1,
    map_zoom: int = 12,
    include_fsbo: bool = True,
    include_new_construction: bool = True,
    include_coming_soon: bool = True,
    include_foreclosures: bool = False,
    include_auctions: bool = False,
) -> dict[str, Any]:
    search_state: dict[str, Any] = {
        "isMapVisible": True,
        "mapBounds": bounds,
        "filterState": build_filter_state(
            status=status,
            home_types=home_types,
            include_fsbo=include_fsbo,
            include_new_construction=include_new_construction,
            include_coming_soon=include_coming_soon,
            include_foreclosures=include_foreclosures,
            include_auctions=include_auctions,
        ),
        "isListVisible": True,
        "mapZoom": map_zoom,
        "usersSearchTerm": location.display,
    }
    if location.region_selection:
        search_state["regionSelection"] = location.region_selection
    if page > 1:
        search_state["pagination"] = {"currentPage": page}

    return {
        "searchQueryState": search_state,
        "wants": {"cat1": ["listResults", "mapResults"]},
        "requestId": random.randint(1, 999),
        "isDebugRequest": False,
    }


async def resolve_search_location(query: str) -> SearchLocation:
    params = {"q": query, "resultTypes": "allRegion", "resultCount": 8}
    async with httpx.AsyncClient(http2=True, timeout=20, follow_redirects=True) as client:
        response = await client.get(AUTOCOMPLETE_ENDPOINT, params=params, headers=SEARCH_HEADERS)
        response.raise_for_status()
        payload = response.json()

    for result in payload.get("results", []):
        metadata = result.get("metaData") or {}
        lat = _to_number(metadata.get("lat"))
        lng = _to_number(metadata.get("lng"))
        if lat is None or lng is None:
            continue
        return SearchLocation(
            query=query,
            display=_to_str(result.get("display")) or query,
            region_id=_to_int(metadata.get("regionId")),
            region_type=_to_str(metadata.get("regionType")),
            city=_to_str(metadata.get("city")),
            county=_to_str(metadata.get("county")),
            state=_to_str(metadata.get("state")),
            zipcode=_to_str(metadata.get("zipCode")),
            country=_to_str(metadata.get("country")),
            latitude=lat,
            longitude=lng,
        )

    raise SearchError(f"Could not resolve Zillow search location: {query}")


def _absolute_zillow_url(url: str, zpid: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return f"https://www.zillow.com{url}"
    if zpid:
        return f"https://www.zillow.com/homedetails/{zpid}_zpid/"
    return url


def _photo_from_listing(card: dict[str, Any]) -> str:
    for key in ("imgSrc", "image", "desktopWebHdpImageLink"):
        value = _to_str(card.get(key))
        if value:
            return value
    for key in ("carouselPhotos", "miniCardPhotos"):
        photos = card.get(key)
        if isinstance(photos, list) and photos:
            value = _to_str((photos[0] or {}).get("url"))
            if value:
                return value
    return _to_str(_nested(card, "hdpData", "homeInfo", "imgSrc"))


def parse_search_listing(card: dict[str, Any]) -> Optional[SearchListingPreview]:
    zpid = _to_id_str(card.get("zpid")) or _to_id_str(card.get("id")) or _to_id_str(card.get("providerListingID"))
    if not zpid:
        return None

    home_info = _nested(card, "hdpData", "homeInfo") or {}
    address_obj = card.get("address") if isinstance(card.get("address"), dict) else {}
    lat_long = card.get("latLong") if isinstance(card.get("latLong"), dict) else {}

    street = (
        _to_str(card.get("addressStreet"))
        or _to_str(address_obj.get("streetAddress"))
        or _to_str(home_info.get("streetAddress"))
        or (_to_str(card.get("address")) if isinstance(card.get("address"), str) else "")
    )
    city = _to_str(card.get("addressCity")) or _to_str(address_obj.get("city")) or _to_str(home_info.get("city"))
    state = _to_str(card.get("addressState")) or _to_str(address_obj.get("state")) or _to_str(home_info.get("state"))
    zipcode = (
        _to_str(card.get("addressZipcode"))
        or _to_str(address_obj.get("zipcode"))
        or _to_str(home_info.get("zipcode"))
    )
    state_zip = " ".join(part for part in [state, zipcode] if part)
    full_address = ", ".join(part for part in [street, city, state_zip] if part)
    raw_url = _to_str(card.get("detailUrl")) or _to_str(home_info.get("homeUrl")) or _to_str(card.get("hdpUrl"))
    url = _absolute_zillow_url(raw_url, zpid)
    variable_data = card.get("variableData") if isinstance(card.get("variableData"), dict) else {}

    price = _to_number(card.get("unformattedPrice")) or _to_number(card.get("price")) or _to_number(home_info.get("price"))
    price_str = _to_str(card.get("price"))
    if not price_str and price is not None:
        price_str = f"${int(price):,}"

    return SearchListingPreview(
        zpid=zpid,
        url=url,
        full_address=full_address,
        street=street,
        city=city,
        state=state,
        zipcode=zipcode,
        price=price,
        price_str=price_str,
        bedrooms=_to_number(card.get("beds")) or _to_number(card.get("bedrooms")) or _to_number(home_info.get("bedrooms")),
        bathrooms=_to_number(card.get("baths")) or _to_number(card.get("bathrooms")) or _to_number(home_info.get("bathrooms")),
        living_area_sqft=_to_number(card.get("area")) or _to_number(card.get("livingArea")) or _to_number(home_info.get("livingArea")),
        lot_size_sqft=_to_number(card.get("lotAreaValue")) or _to_number(home_info.get("lotAreaValue")) or _to_number(home_info.get("lotSize")),
        home_type=_to_str(card.get("homeType")) or _to_str(home_info.get("homeType")),
        listing_status=_to_str(card.get("statusText")) or _to_str(card.get("homeStatus")) or _to_str(home_info.get("homeStatus")),
        days_on_zillow=_to_number(card.get("daysOnZillow")) or _to_number(card.get("daysOnMarket")),
        latitude=_to_number(lat_long.get("latitude")) or _to_number(card.get("latitude")) or _to_number(home_info.get("latitude")),
        longitude=_to_number(lat_long.get("longitude")) or _to_number(card.get("longitude")) or _to_number(home_info.get("longitude")),
        image=_photo_from_listing(card),
        description=_to_str(card.get("description")) or _to_str(variable_data.get("text")),
    )


def extract_search_listings(payload: dict[str, Any]) -> tuple[list[SearchListingPreview], Optional[int]]:
    cat1 = payload.get("cat1") if isinstance(payload.get("cat1"), dict) else {}
    search_results = cat1.get("searchResults") if isinstance(cat1.get("searchResults"), dict) else {}
    total = _to_int(search_results.get("totalResultCount"))

    cards: list[dict[str, Any]] = []
    for key in ("listResults", "mapResults", "relaxedResults"):
        value = search_results.get(key)
        if isinstance(value, list):
            cards.extend(card for card in value if isinstance(card, dict))

    by_zpid: dict[str, SearchListingPreview] = {}
    for card in cards:
        listing = parse_search_listing(card)
        if listing is not None:
            by_zpid.setdefault(listing.zpid, listing)
    return list(by_zpid.values()), total


def listing_matches_location(listing: SearchListingPreview, location: SearchLocation) -> bool:
    region_type = (location.region_type or "").lower()
    if region_type == "city" and location.city:
        if listing.city.lower() != location.city.lower():
            return False
        if location.state and listing.state.lower() != location.state.lower():
            return False
    if region_type == "zipcode" and location.zipcode:
        return listing.zipcode == location.zipcode
    if region_type == "state" and location.state:
        return listing.state.lower() == location.state.lower()
    return True


def filter_listings_to_location(
    listings: list[SearchListingPreview],
    location: SearchLocation,
) -> tuple[list[SearchListingPreview], int]:
    kept = [item for item in listings if listing_matches_location(item, location)]
    return kept, len(listings) - len(kept)


async def _fetch_search_payload(
    *,
    pool: CookiePool,
    payload: dict[str, Any],
    referer: str,
    max_retries: int,
) -> tuple[dict[str, Any], int]:
    last_error = ""
    body = json.dumps(payload)

    for _ in range(max_retries):
        slot = await pool.acquire()
        headers = dict(SEARCH_HEADERS)
        headers["referer"] = referer
        try:
            async with slot.sem:
                response = await slot.client.put(
                    ZILLOW_SEARCH_ENDPOINT,
                    content=body,
                    headers=headers,
                    cookies=slot.cookies,
                    timeout=30,
                )
            if response.status_code == 200:
                return response.json(), len(response.content)
            if response.status_code == 403:
                pool.mark_dead(slot)
                last_error = "Zillow search returned 403; cookie slot marked dead"
                continue
            last_error = f"Zillow search returned HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:
            last_error = f"Zillow search request failed: {type(exc).__name__}: {exc}"
        await asyncio.sleep(0.25)

    raise SearchError(last_error or "Zillow search failed")


async def discover_zillow_listings(
    *,
    pool: CookiePool,
    location_query: str,
    status: str = "for_sale",
    home_types: Optional[list[str]] = None,
    max_results: int = 500,
    radius_miles: float = 12.0,
    tile_if_needed: bool = True,
    tile_depth: int = 2,
    search_concurrency: int = 4,
    max_retries: int = 3,
    include_fsbo: bool = True,
    include_new_construction: bool = True,
    include_coming_soon: bool = True,
    include_foreclosures: bool = False,
    include_auctions: bool = False,
    strict_location: bool = True,
) -> tuple[list[SearchListingPreview], SearchLocation, SearchStats]:
    t0 = time.monotonic()
    home_types = home_types or ["multi_family"]
    location = await resolve_search_location(location_query)
    if location.latitude is None or location.longitude is None:
        raise SearchError(f"Resolved location has no coordinates: {location_query}")

    root_bounds = bounds_for_radius(location.latitude, location.longitude, radius_miles)
    referer = _search_referer(location, home_types)
    stats = SearchStats()

    async def fetch_bounds(bounds: dict[str, float]) -> tuple[list[SearchListingPreview], Optional[int]]:
        payload = build_search_payload(
            location=location,
            bounds=bounds,
            status=status,
            home_types=home_types,
            include_fsbo=include_fsbo,
            include_new_construction=include_new_construction,
            include_coming_soon=include_coming_soon,
            include_foreclosures=include_foreclosures,
            include_auctions=include_auctions,
        )
        data, _size = await _fetch_search_payload(
            pool=pool, payload=payload, referer=referer, max_retries=max_retries,
        )
        stats.search_requests += 1
        return extract_search_listings(data)

    listings, total = await fetch_bounds(root_bounds)
    stats.total_result_count = total

    should_tile = (
        tile_if_needed
        and tile_depth > 0
        and total is not None
        and len(listings) < min(total, max_results)
    )
    if should_tile:
        stats.tiled = True
        bounds_queue = split_bounds(root_bounds)
        by_zpid = {item.zpid: item for item in listings}
        sem = asyncio.Semaphore(search_concurrency)

        async def fetch_tile(bounds: dict[str, float]) -> None:
            async with sem:
                tile_listings, _tile_total = await fetch_bounds(bounds)
                for item in tile_listings:
                    by_zpid.setdefault(item.zpid, item)

        for _depth in range(tile_depth):
            await asyncio.gather(*(fetch_tile(bounds) for bounds in bounds_queue))
            if len(by_zpid) >= min(total, max_results):
                break
            bounds_queue = [child for bounds in bounds_queue for child in split_bounds(bounds)]
        listings = list(by_zpid.values())

    if strict_location:
        listings, discarded = filter_listings_to_location(listings, location)
        stats.discarded_out_of_location = discarded

    stats.discovered = len(listings)
    stats.elapsed_s = time.monotonic() - t0
    return listings[:max_results], location, stats

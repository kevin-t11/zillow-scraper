"""Zillow search/discovery helpers for batch listing collection.

This module collects fresh listing previews from Zillow search/map results.
Discovery is independent of the property-detail scraper and its cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, asdict, field, fields
from typing import Any, Iterable, Optional

import httpx

from .cookie_pool import CookiePool, CookieSlot, PoolUnavailableError, SessionValidationError

logger = logging.getLogger("px_bypass.search")

AUTOCOMPLETE_ENDPOINT = "https://www.zillowstatic.com/autocomplete/v3/suggestions"
ZILLOW_SEARCH_ENDPOINT = "https://www.zillow.com/async-create-search-page-state"
EARTH_RADIUS_MILES = 3958.7613

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


class SearchCookiePool(CookiePool):
    """Validate discovery independently of Zillow's property-detail service."""

    async def _validate_slot(self, slot: CookieSlot) -> None:
        payload = build_search_payload(
            location=None,
            bounds={"north": 41.78, "south": 41.75, "east": -88.13, "west": -88.17},
            status="for_sale", home_types=["single_family"], strict_location=False,
        )
        response = await slot.client.put(
            ZILLOW_SEARCH_ENDPOINT, json=payload,
            headers={**SEARCH_HEADERS, "referer": "https://www.zillow.com/homes/"}, timeout=15,
        )
        if response.status_code != 200:
            raise SessionValidationError(f"Validation search returned HTTP {response.status_code}")
        try:
            valid = isinstance(_nested(response.json(), "cat1", "searchResults"), dict)
        except ValueError:
            valid = False
        if not valid:
            raise SessionValidationError("Validation search did not contain search results")
        slot.last_success_at = time.time()


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
    discarded_out_of_area: int = 0
    discarded_missing_coordinates: int = 0
    coverage_limited: bool = False
    limit_reached: bool = False
    area: dict[str, Any] = field(default_factory=dict)


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
        if not math.isfinite(parsed):
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
    angular_radius = radius_miles / EARTH_RADIUS_MILES
    lat_delta = math.degrees(angular_radius)
    south, north = max(-90, latitude - lat_delta), min(90, latitude + lat_delta)
    if south == -90 or north == 90:
        west, east = -180.0, 180.0
    else:
        lng_delta = math.degrees(math.asin(min(1, math.sin(angular_radius) / math.cos(math.radians(latitude)))))
        west, east = longitude - lng_delta, longitude + lng_delta
        if west < -180 or east > 180:
            raise SearchError("This radius crosses the antimeridian; use two separate bbox searches")
    return {
        "north": north, "south": south, "east": east, "west": west,
    }


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1, max(0, a))))


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
    new_construction_only: bool = False,
    year_built_min: Optional[int] = None,
    year_built_max: Optional[int] = None,
    sold_in_last: Optional[str] = None,
) -> dict[str, Any]:
    status = status.lower()
    if status not in {"for_sale", "for_rent", "sold"}:
        raise ValueError(f"unsupported search status: {status}")
    for_sale = status == "for_sale"
    ordinary_sale = for_sale and not new_construction_only
    filters = {
        "isForSaleByAgent": {"value": ordinary_sale},
        "isForSaleByOwner": {"value": ordinary_sale and include_fsbo},
        "isNewConstruction": {"value": for_sale and (include_new_construction or new_construction_only)},
        "isComingSoon": {"value": ordinary_sale and include_coming_soon},
        "isAuction": {"value": ordinary_sale and include_auctions},
        "isForSaleForeclosure": {"value": ordinary_sale and include_foreclosures},
        "isForRent": {"value": status == "for_rent"},
        "isRecentlySold": {"value": status == "sold"},
        "isActiveStatus": {"value": for_sale},
        "isComingSoonStatus": {"value": for_sale and include_coming_soon},
        "isZillowPreview": {"value": for_sale and include_coming_soon},
        "sortSelection": {"value": "newest" if for_sale else "globalrelevanceex"},
        **build_home_type_filter(home_types),
    }
    built = {}
    if year_built_min is not None:
        built["min"] = year_built_min
    if year_built_max is not None:
        built["max"] = year_built_max
    if built:
        filters["built"] = built
    if sold_in_last is not None:
        filters["doz"] = {"value": sold_in_last}
    return filters


def _search_referer(location: Optional[SearchLocation], home_types: Iterable[str]) -> str:
    if location is None:
        return "https://www.zillow.com/homes/"
    slug = re.sub(r"[^a-z0-9]+", "-", location.display.lower()).strip("-")
    if "multi_family" in set(home_types):
        return f"https://www.zillow.com/{slug}/multi-family-home_att/"
    return f"https://www.zillow.com/{slug}_rb/"


def build_search_payload(
    *,
    location: Optional[SearchLocation],
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
    strict_location: bool = True,
    new_construction_only: bool = False,
    year_built_min: Optional[int] = None,
    year_built_max: Optional[int] = None,
    sold_in_last: Optional[str] = None,
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
            new_construction_only=new_construction_only,
            year_built_min=year_built_min,
            year_built_max=year_built_max,
            sold_in_last=sold_in_last,
        ),
        "isListVisible": True,
        "mapZoom": map_zoom,
    }
    if location is not None and strict_location:
        search_state["usersSearchTerm"] = location.display
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
    try:
        async with httpx.AsyncClient(http2=True, timeout=20, follow_redirects=True) as client:
            response = await client.get(AUTOCOMPLETE_ENDPOINT, params=params, headers=SEARCH_HEADERS)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchError(f"Zillow location lookup failed ({type(exc).__name__})") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchError("Zillow location lookup returned an unexpected response")

    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        metadata = result.get("metaData") or {}
        if not isinstance(metadata, dict):
            continue
        lat = _to_number(metadata.get("lat"))
        lng = _to_number(metadata.get("lng"))
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
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
        latitude=next((n for v in (lat_long.get("latitude"), card.get("latitude"), home_info.get("latitude")) if (n := _to_number(v)) is not None), None),
        longitude=next((n for v in (lat_long.get("longitude"), card.get("longitude"), home_info.get("longitude")) if (n := _to_number(v)) is not None), None),
        image=_photo_from_listing(card),
        description=_to_str(card.get("description")) or _to_str(variable_data.get("text")),
    )


def extract_search_listings(payload: dict[str, Any]) -> tuple[list[SearchListingPreview], Optional[int]]:
    cat1 = payload.get("cat1") if isinstance(payload.get("cat1"), dict) else {}
    search_results = cat1.get("searchResults") if isinstance(cat1.get("searchResults"), dict) else {}
    total = _to_int(search_results.get("totalResultCount"))
    if total is None:
        total = _to_int(_nested(cat1, "searchList", "totalResultCount"))

    cards: list[dict[str, Any]] = []
    # Nearby/relaxed matches may violate ANY requested filter, not just geography.
    for key in ("listResults", "mapResults"):
        value = search_results.get(key)
        if isinstance(value, list):
            cards.extend(card for card in value if isinstance(card, dict))

    by_zpid: dict[str, SearchListingPreview] = {}
    for card in cards:
        listing = parse_search_listing(card)
        if listing is not None:
            existing = by_zpid.get(listing.zpid)
            if existing is None:
                by_zpid[listing.zpid] = listing
            else:
                # List cards may lack coordinates that their map card supplies.
                for item in fields(listing):
                    if getattr(existing, item.name) in (None, ""):
                        setattr(existing, item.name, getattr(listing, item.name))
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

    for attempt in range(max_retries):
        headers = dict(SEARCH_HEADERS)
        headers["referer"] = referer
        try:
            async with pool.lease() as slot:
                async with slot.sem:
                    response = await slot.client.put(
                        ZILLOW_SEARCH_ENDPOINT,
                        content=body,
                        headers=headers,
                        cookies=slot.cookies,
                        timeout=30,
                    )
            if response.status_code == 200:
                data = response.json()
                if not isinstance(_nested(data, "cat1", "searchResults"), dict):
                    raise ValueError("Missing search results")
                slot.last_success_at = time.time()
                return data, len(response.content)
            if response.status_code == 403:
                pool.mark_dead(slot)
                last_error = "Zillow search returned 403; cookie slot marked dead"
                continue
            last_error = f"Zillow search returned HTTP {response.status_code}"
        except PoolUnavailableError as exc:
            raise SearchError("No validated scraping session is available for search") from exc
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"Zillow search request failed ({type(exc).__name__})"
        if attempt + 1 < max_retries:
            await asyncio.sleep(0.25)

    raise SearchError(last_error or "Zillow search failed")


def listing_area_rejection(
    listing: SearchListingPreview,
    bounds: dict[str, float],
    center: Optional[tuple[float, float]] = None,
    radius_miles: Optional[float] = None,
) -> Optional[str]:
    lat, lon = listing.latitude, listing.longitude
    if lat is None or lon is None or not math.isfinite(lat) or not math.isfinite(lon):
        return "missing_coordinates"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "missing_coordinates"
    if not (bounds["south"] <= lat <= bounds["north"] and bounds["west"] <= lon <= bounds["east"]):
        return "out_of_area"
    if center is not None and radius_miles is not None:
        if distance_miles(*center, lat, lon) > radius_miles + 1e-8:
            return "out_of_area"
    return None


async def discover_zillow_listings(
    *,
    pool: CookiePool,
    location_query: Optional[str] = None,
    bbox: Optional[dict[str, float]] = None,
    status: str = "for_sale",
    home_types: Optional[list[str]] = None,
    max_results: int = 500,
    radius_miles: float = 12.0,
    tile_if_needed: bool = True,
    tile_depth: int = 2,
    search_concurrency: int = 4,
    max_search_requests: int = 25,
    max_retries: int = 3,
    include_fsbo: bool = True,
    include_new_construction: bool = True,
    include_coming_soon: bool = True,
    include_foreclosures: bool = False,
    include_auctions: bool = False,
    strict_location: bool = False,
    new_construction_only: bool = False,
    year_built_min: Optional[int] = None,
    year_built_max: Optional[int] = None,
    sold_in_last: Optional[str] = None,
) -> tuple[list[SearchListingPreview], Optional[SearchLocation], SearchStats]:
    t0 = time.monotonic()
    home_types = home_types or ["multi_family"]
    if (location_query is None) == (bbox is None):
        raise ValueError("Provide exactly one of location_query or bbox")
    location = await resolve_search_location(location_query) if location_query is not None else None
    center = None
    if location is not None:
        if location.latitude is None or location.longitude is None:
            raise SearchError(f"Resolved location has no coordinates: {location_query}")
        if strict_location and not location.region_selection:
            raise SearchError("Resolved location has no Zillow region boundary; use strict_location=false")
        center = (location.latitude, location.longitude)
        root_bounds = bounds_for_radius(*center, radius_miles)
    else:
        root_bounds = dict(bbox)
    referer = _search_referer(location if strict_location else None, home_types)
    stats = SearchStats(area={
        "mode": "location_radius" if center else "bbox",
        "bbox": root_bounds,
        "center": {"latitude": center[0], "longitude": center[1]} if center else None,
        "radius_miles": radius_miles if center else None,
        "strict_location": bool(location and strict_location),
        "region_selection": location.region_selection if location and strict_location else [],
    })
    by_zpid: dict[str, SearchListingPreview] = {}
    rejected: dict[str, set[str]] = {
        "missing_coordinates": set(), "out_of_area": set(), "out_of_location": set(),
    }

    async def fetch_bounds(bounds: dict[str, float], depth: int, page: int):
        payload = build_search_payload(
            location=location, bounds=bounds, status=status, home_types=home_types,
            page=page, strict_location=strict_location,
            include_fsbo=include_fsbo, include_new_construction=include_new_construction,
            include_coming_soon=include_coming_soon, include_foreclosures=include_foreclosures,
            include_auctions=include_auctions, new_construction_only=new_construction_only,
            year_built_min=year_built_min, year_built_max=year_built_max, sold_in_last=sold_in_last,
        )
        data, _size = await _fetch_search_payload(
            pool=pool, payload=payload, referer=referer, max_retries=max_retries,
        )
        return bounds, depth, page, data

    # Only subdivide boxes that were truncated. Paginate dense leaf boxes and
    # bound the entire discovery run, rather than refetching every quadrant.
    queue = [(root_bounds, 0, 1)]
    seen_by_bounds: dict[tuple[float, ...], set[str]] = {}

    def record_response(response):
        bounds, depth, page, data = response
        stats.tiled = stats.tiled or depth > 0
        listings, total = extract_search_listings(data)
        if depth == 0 and page == 1:
            stats.total_result_count = total
        bounds_key = tuple(bounds[key] for key in ("south", "west", "north", "east"))
        seen = seen_by_bounds.setdefault(bounds_key, set())
        new_ids = {item.zpid for item in listings} - seen
        seen.update(new_ids)
        for listing in listings:
            reason = listing_area_rejection(listing, root_bounds, center, radius_miles)
            if reason is None and strict_location and location and not listing_matches_location(listing, location):
                reason = "out_of_location"
            if reason:
                rejected[reason].add(listing.zpid)
            else:
                by_zpid.setdefault(listing.zpid, listing)

        total_pages = _to_int(_nested(data, "cat1", "searchList", "totalPages"))
        incomplete = total > len(seen) if total is not None else bool(total_pages and page < total_pages)
        if total is None and total_pages is None:
            stats.coverage_limited = True
        if not incomplete:
            return
        if page == 1 and tile_if_needed and depth < tile_depth:
            queue.extend((child, depth + 1, 1) for child in split_bounds(bounds))
        elif new_ids and (total_pages is None or page < total_pages):
            queue.append((bounds, depth, page + 1))
        else:
            # Zillow can cap/duplicate pages or include non-property cards
            # in its count. Do not claim the area was completely enumerated.
            stats.coverage_limited = True

    while queue and len(by_zpid) < max_results and stats.search_requests < max_search_requests:
        count = min(search_concurrency, max_search_requests - stats.search_requests, len(queue))
        batch, queue = queue[:count], queue[count:]
        tasks = [asyncio.create_task(fetch_bounds(*job)) for job in batch]
        stats.search_requests += len(tasks)
        try:
            for completed in asyncio.as_completed(tasks):
                record_response(await completed)
                if len(by_zpid) >= max_results:
                    break
        finally:
            # A slow quadrant must not hold up a complete result set. Await
            # cancellation so pooled connections and slot leases are released.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    stats.limit_reached = len(by_zpid) >= max_results
    if queue and not stats.limit_reached:
        stats.coverage_limited = True
    stats.discarded_out_of_area = len(rejected["out_of_area"] - by_zpid.keys())
    stats.discarded_missing_coordinates = len(rejected["missing_coordinates"] - by_zpid.keys())
    stats.discarded_out_of_location = len(rejected["out_of_location"] - by_zpid.keys())
    stats.discovered = len(by_zpid)
    stats.elapsed_s = time.monotonic() - t0
    return list(by_zpid.values())[:max_results], location, stats

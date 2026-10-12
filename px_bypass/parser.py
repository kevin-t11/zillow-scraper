"""
Map a Zillow data.property response into PropertyListing.

Field paths were derived from a real 200-response captured in the HAR.
This parser is intentionally tolerant — every field falls back to a sane
default if the upstream JSON is missing the path.
"""

from __future__ import annotations

from typing import Any, Optional

from app.models import PropertyListing


def _g(d: dict, *path, default=None) -> Any:
    cur = d
    for p in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and 0 <= p < len(cur):
            cur = cur[p]
        else:
            return default
    return cur if cur is not None else default


def _highest_res_photo(p: dict) -> Optional[str]:
    """originalPhotos[i] → highest-resolution jpeg URL."""
    sources = p.get("mixedSources") or {}
    for codec in ("jpeg", "webp"):
        arr = sources.get(codec) or []
        if arr:
            return max(arr, key=lambda s: s.get("width", 0)).get("url")
    return None


def _format_price(price: Optional[float]) -> str:
    if not price:
        return ""
    return f"${int(price):,}"


def _parse_nearby_home(h: dict) -> dict:
    addr = h.get("address") or {}
    photo = ""
    cards = h.get("miniCardPhotos") or []
    if cards and isinstance(cards, list):
        photo = (cards[0] or {}).get("url", "")
    return {
        "zpid": str(h.get("zpid", "")),
        "price": h.get("price"),
        "price_str": _format_price(h.get("price")),
        "bedrooms": h.get("bedrooms"),
        "bathrooms": h.get("bathrooms"),
        "living_area": h.get("livingArea"),
        "address": (
            f"{addr.get('streetAddress', '')}, "
            f"{addr.get('city', '')}, "
            f"{addr.get('state', '')} "
            f"{addr.get('zipcode', '')}"
        ).strip(", "),
        "street": addr.get("streetAddress", "") or "",
        "city": addr.get("city", "") or "",
        "state": addr.get("state", "") or "",
        "zipcode": addr.get("zipcode", "") or "",
        "url": h.get("hdpUrl", "") or "",
        "image": photo,
        "status": h.get("homeStatus", "") or "",
        "latitude": h.get("latitude"),
        "longitude": h.get("longitude"),
        "source_data": h,
    }


def _parse_school(s: dict) -> dict:
    return {
        "name": s.get("name", "") or "",
        "rating": s.get("rating"),
        "grades": s.get("grades", "") or "",
        "distance": s.get("distance"),
        "level": s.get("level", "") or "",
        "link": s.get("link", "") or "",
    }


def _parse_price_event(e: dict) -> dict:
    return {
        "date": e.get("date", "") or "",
        "price": e.get("price"),
        "event": e.get("event", "") or "",
        "source": e.get("source", "") or "",
        "price_per_sqft": e.get("pricePerSquareFoot"),
        "price_change_rate": e.get("priceChangeRate"),
        "source_data": e,
    }


def _parse_tax_event(e: dict) -> dict:
    # `time` is a unix-ms epoch.
    import datetime as dt
    iso = ""
    if e.get("time"):
        try:
            iso = dt.datetime.utcfromtimestamp(e["time"] / 1000).date().isoformat()
        except Exception:
            iso = ""
    return {
        "date": iso,
        "tax_paid": e.get("taxPaid"),
        "tax_increase_rate": e.get("taxIncreaseRate"),
        "value": e.get("value"),
        "value_increase_rate": e.get("valueIncreaseRate"),
        "source_data": e,
    }


def parse_for_sale_non_priority(
    resp_json: dict,
    *,
    input_address: str = "",
    listing_url: str = "",
) -> PropertyListing:
    p = _g(resp_json, "data", "property") or {}
    if not p:
        return PropertyListing(
            input_address=input_address,
            status="parse_error",
            error_msg="no data.property in response",
        )

    addr = p.get("address") or {}
    reso = p.get("resoFacts") or {}
    attr = p.get("attributionInfo") or {}

    # Photos: originalPhotos[i].mixedSources.jpeg[max width].url
    orig = p.get("originalPhotos") or []
    photos = [u for u in (_highest_res_photo(o) for o in orig) if u]
    if not photos:
        photos = [u for u in (
            (rp or {}).get("url") for rp in (p.get("responsivePhotos") or [])
        ) if u]

    # Price-history → list of slim events.
    price_history = [_parse_price_event(e) for e in (p.get("priceHistory") or [])]

    # Last-sold = most recent "Sold" event in price history (fallback to top-level).
    last_sold_price = p.get("lastSoldPrice")
    last_sold_date = ""
    for e in reversed(price_history):
        if "Sold" in (e.get("event") or "") and e.get("price"):
            last_sold_price = last_sold_price or e["price"]
            last_sold_date = e.get("date", "") or last_sold_date
            break

    # Tax history.
    tax_history = [_parse_tax_event(e) for e in (p.get("taxHistory") or [])]

    # Nearby homes (already in the same response).
    nearby = [_parse_nearby_home(h) for h in (p.get("nearbyHomes") or [])]

    # Parking
    parking_features = list(reso.get("parkingFeatures") or [])
    parking_capacity = reso.get("parkingCapacity")
    garage_capacity = reso.get("garageParkingCapacity") or reso.get("coveredParkingCapacity")

    # HOA
    hoa = reso.get("hoaFee") or reso.get("hoaFeeTotal") or p.get("monthlyHoaFee")

    # Listing date / sold date
    listing_date = p.get("datePostedString") or reso.get("onMarketDate") or ""

    # MLS / agents
    agent_name = attr.get("agentName") or ""
    agent_phone = attr.get("agentPhoneNumber") or ""
    agent_email = attr.get("agentEmail") or ""
    broker_name = attr.get("brokerName") or p.get("brokerageName") or ""
    listing_offices = attr.get("listingOffices") or []
    listing_office = ""
    if isinstance(listing_offices, list) and listing_offices:
        first = listing_offices[0] or {}
        listing_office = first.get("officeName") or ""
    mls_id = attr.get("mlsId") or ""

    return PropertyListing(
        input_address=input_address or listing_url,
        status="success",
        error_msg="",
        attempts=1,

        zpid=str(p.get("zpid", "")),
        full_address=(
            f"{addr.get('streetAddress', '')}, "
            f"{addr.get('city', '')}, "
            f"{addr.get('state', '')} "
            f"{addr.get('zipcode', '')}"
        ).strip(", "),
        street=addr.get("streetAddress", "") or "",
        city=addr.get("city", "") or "",
        state=addr.get("state", "") or "",
        zipcode=addr.get("zipcode", "") or "",
        url=listing_url or f"https://www.zillow.com{p.get('hdpUrl','')}",

        price=p.get("price"),
        price_str=_format_price(p.get("price")),
        zestimate=p.get("zestimate"),
        rent_zestimate=p.get("rentZestimate"),
        bedrooms=p.get("bedrooms"),
        bathrooms=p.get("bathrooms"),
        living_area_sqft=p.get("livingAreaValue") or p.get("livingArea"),
        lot_size_sqft=p.get("lotSize"),
        home_type=p.get("homeType", "") or "",
        year_built=p.get("yearBuilt") or reso.get("yearBuilt"),

        parking_spaces=parking_capacity,
        garage_spaces=garage_capacity,
        has_cooling=reso.get("hasCooling"),
        has_heating=reso.get("hasHeating"),
        heating_type=", ".join(reso.get("heating") or []) if isinstance(reso.get("heating"), list) else (reso.get("heating") or ""),
        cooling_type=", ".join(reso.get("cooling") or []) if isinstance(reso.get("cooling"), list) else (reso.get("cooling") or ""),

        listing_status=p.get("homeStatus", "") or "",
        days_on_zillow=p.get("daysOnZillow"),
        views=p.get("pageViewCount"),
        saves=p.get("favoriteCount"),
        listing_date=listing_date,
        sold_date=last_sold_date,

        agent_name=agent_name,
        agent_phone=agent_phone,
        agent_email=agent_email,
        broker_name=broker_name,
        listing_office=listing_office,
        mls_id=mls_id,

        last_sold_price=last_sold_price,
        last_sold_date=last_sold_date,

        monthly_hoa_fee=hoa,
        property_tax_rate=reso.get("propertyTaxRate"),
        price_per_sqft=reso.get("pricePerSquareFoot"),
        annual_tax=reso.get("taxAnnualAmount"),
        tax_year=None,

        mortgage_rate_30yr=None,
        mortgage_rate_15yr=None,
        mortgage_rate_arm5=None,

        description=p.get("description", "") or "",
        highlights=list(p.get("highlights") or []),
        appliances=list(reso.get("appliances") or []),
        interior_features=list(reso.get("interiorFeatures") or []),
        exterior_features=list(reso.get("exteriorFeatures") or []),
        community_features=list(reso.get("communityFeatures") or []),
        parking_features=parking_features,

        latitude=p.get("latitude"),
        longitude=p.get("longitude"),
        neighborhood=(addr.get("neighborhood") or "") or "",
        county=p.get("county", "") or "",

        photo_count=p.get("photoCount") or len(photos),
        photos=photos[:50],
        virtual_tour_url=(p.get("thirdPartyVirtualTour") or {}).get("externalUrl", "") or "",

        schools=[_parse_school(s) for s in (p.get("schools") or [])],
        price_history=price_history,
        tax_history=tax_history,
        nearby_homes=nearby,
    )

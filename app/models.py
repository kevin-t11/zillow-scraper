"""
models.py — Data models (Pydantic for API, dataclass for internal)
"""

from copy import deepcopy
from dataclasses import dataclass, field, asdict, fields
from typing import Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# API Request/Response models (Pydantic)
# ─────────────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    addresses: list[str]
    concurrency: int = Field(default=30, ge=1, le=200)
    retries: int = Field(default=3, ge=1, le=10)
    batch_size: int = Field(default=100, ge=1, le=1000)


class UrlScrapeRequest(BaseModel):
    urls: list[str]
    concurrency: int = Field(default=50, ge=1, le=200)
    retries: int = Field(default=2, ge=1, le=5)


class JobStatus(BaseModel):
    job_id: str
    status: str  # running | completed | failed
    total: int
    completed: int
    success: int
    errors: int
    elapsed: float


# ─────────────────────────────────────────────────────────────────────────────
# Internal data model (dataclass — used by scraper)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PropertyListing:
    input_address: str = ""
    status: str = ""            # success | partial | not_found | error | parse_error
    error_msg: str = ""
    attempts: int = 0

    zpid: str = ""
    full_address: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""
    url: str = ""

    price: Optional[float] = None
    price_str: str = ""
    zestimate: Optional[float] = None
    rent_zestimate: Optional[float] = None

    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    living_area_sqft: Optional[float] = None
    lot_size_sqft: Optional[float] = None
    home_type: str = ""
    year_built: Optional[int] = None
    parking_spaces: Optional[int] = None
    garage_spaces: Optional[int] = None
    has_cooling: Optional[bool] = None
    has_heating: Optional[bool] = None
    heating_type: str = ""
    cooling_type: str = ""

    listing_status: str = ""
    days_on_zillow: Optional[int] = None
    views: Optional[int] = None
    saves: Optional[int] = None
    listing_date: str = ""
    sold_date: str = ""

    agent_name: str = ""
    agent_phone: str = ""
    agent_email: str = ""
    broker_name: str = ""
    listing_office: str = ""
    mls_id: str = ""

    last_sold_price: Optional[float] = None
    last_sold_date: str = ""
    monthly_hoa_fee: Optional[float] = None
    property_tax_rate: Optional[float] = None
    price_per_sqft: Optional[float] = None
    annual_tax: Optional[float] = None
    tax_year: Optional[int] = None

    mortgage_rate_30yr: Optional[float] = None
    mortgage_rate_15yr: Optional[float] = None
    mortgage_rate_arm5: Optional[float] = None

    description: str = ""
    highlights: list = field(default_factory=list)
    appliances: list = field(default_factory=list)
    interior_features: list = field(default_factory=list)
    exterior_features: list = field(default_factory=list)
    community_features: list = field(default_factory=list)
    parking_features: list = field(default_factory=list)

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    neighborhood: str = ""
    county: str = ""

    photo_count: int = 0
    photos: list = field(default_factory=list)
    virtual_tour_url: str = ""

    schools: list = field(default_factory=list)
    price_history: list = field(default_factory=list)
    tax_history: list = field(default_factory=list)
    nearby_homes: list = field(default_factory=list)

    raw_json: dict = field(default_factory=dict, repr=False)

    def to_dict(self, include_raw: bool = False) -> dict:
        if include_raw:
            return asdict(self)
        # Avoid copying a potentially large upstream payload just to discard it.
        return {f.name: deepcopy(getattr(self, f.name))
                for f in fields(self) if f.name != "raw_json"}

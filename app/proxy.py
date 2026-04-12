"""
proxy.py — BrightData proxy integration (httpx-native)
"""

import os
import random
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger("proxy")


@dataclass
class BrightDataConfig:
    customer_id: str
    zone_name: str
    password: str
    host: str = "brd.superproxy.io"
    port: int = 33335
    country: str = "us"
    state: Optional[str] = None


class ProxyManager:
    def __init__(self, config: BrightDataConfig):
        self.config = config
        self._session_counter = 0

    @classmethod
    def from_env(cls) -> "ProxyManager":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        customer_id = os.getenv("BRIGHTDATA_CUSTOMER_ID", "")
        password = os.getenv("BRIGHTDATA_PASSWORD", "")
        if not customer_id or not password:
            raise ValueError(
                "Set BRIGHTDATA_CUSTOMER_ID and BRIGHTDATA_PASSWORD in .env"
            )

        return cls(BrightDataConfig(
            customer_id=customer_id,
            zone_name=os.getenv("BRIGHTDATA_ZONE", "zillow_web_unlocker"),
            password=password,
            host=os.getenv("BRIGHTDATA_HOST", "brd.superproxy.io"),
            port=int(os.getenv("BRIGHTDATA_PORT", "33335")),
            country=os.getenv("BRIGHTDATA_COUNTRY", "us"),
            state=os.getenv("BRIGHTDATA_STATE", "") or None,
        ))

    @classmethod
    def from_settings(cls, settings) -> "ProxyManager":
        return cls(BrightDataConfig(
            customer_id=settings.brightdata_customer_id,
            zone_name=settings.brightdata_zone,
            password=settings.brightdata_password,
            host=settings.brightdata_host,
            port=settings.brightdata_port,
            country=settings.brightdata_country,
            state=settings.brightdata_state,
        ))

    def _build_username(self, session_id: Optional[str] = None) -> str:
        c = self.config
        username = f"brd-customer-{c.customer_id}-zone-{c.zone_name}"
        if c.country:
            username += f"-country-{c.country}"
        if c.state:
            username += f"-state-{c.state}"
        if session_id:
            username += f"-session-{session_id}"
        return username

    def get_proxy_url(self, sticky: bool = False) -> str:
        c = self.config
        session_id = None
        if sticky:
            self._session_counter += 1
            session_id = f"z{self._session_counter}_{random.randint(1000, 9999)}"

        username = self._build_username(session_id=session_id)
        pw = quote(c.password, safe="")
        return f"http://{username}:{pw}@{c.host}:{c.port}"

    def is_configured(self) -> bool:
        return bool(self.config.customer_id and self.config.password)

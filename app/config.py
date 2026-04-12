"""
config.py — App settings from environment / .env
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    # BrightData
    brightdata_customer_id: str = ""
    brightdata_zone: str = "zillow_web_unlocker"
    brightdata_password: str = ""
    brightdata_host: str = "brd.superproxy.io"
    brightdata_port: int = 33335
    brightdata_country: str = "us"
    brightdata_state: Optional[str] = None

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Scraper defaults
    default_concurrency: int = 30
    default_retries: int = 3
    batch_size: int = 100  # stream results every N completions


def load_settings(dotenv_path: str = ".env") -> Settings:
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)
    except ImportError:
        pass

    return Settings(
        brightdata_customer_id=os.getenv("BRIGHTDATA_CUSTOMER_ID", ""),
        brightdata_zone=os.getenv("BRIGHTDATA_ZONE", "zillow_web_unlocker"),
        brightdata_password=os.getenv("BRIGHTDATA_PASSWORD", ""),
        brightdata_host=os.getenv("BRIGHTDATA_HOST", "brd.superproxy.io"),
        brightdata_port=int(os.getenv("BRIGHTDATA_PORT", "33335")),
        brightdata_country=os.getenv("BRIGHTDATA_COUNTRY", "us"),
        brightdata_state=os.getenv("BRIGHTDATA_STATE", "") or None,
        host=os.getenv("SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SERVER_PORT", "8000")),
        default_concurrency=int(os.getenv("DEFAULT_CONCURRENCY", "30")),
        default_retries=int(os.getenv("DEFAULT_RETRIES", "3")),
        batch_size=int(os.getenv("BATCH_SIZE", "100")),
    )

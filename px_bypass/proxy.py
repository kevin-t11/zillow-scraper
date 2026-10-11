"""
Proxy URL builders.

Two backends:
  - Evomi residential (sticky session per scraper run)
  - Webshare rotating (DC or rotating residential, IP rotates per request)
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse, unquote

from dotenv import load_dotenv


# ──────────────────────────────────────────────────────────────────────────
# Evomi
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvomiConfig:
    host: str
    port: int
    username: str
    password: str
    scheme: str = "http"

    @classmethod
    def from_env(cls) -> "EvomiConfig":
        load_dotenv()
        host = os.getenv("EVOMI_HOST", "rp.evomi.com")
        port = int(os.getenv("EVOMI_PORT", "1000"))
        scheme = os.getenv("EVOMI_SCHEME", "https" if port == 1001 else "http").lower()
        if scheme not in {"http", "https"}:
            raise ValueError("EVOMI_SCHEME must be http or https")
        user = os.getenv("EVOMI_USERNAME") or ""
        pw = os.getenv("EVOMI_PASSWORD") or ""
        if not user or not pw:
            raise SystemExit("Set EVOMI_USERNAME and EVOMI_PASSWORD in .env")
        return cls(host=host, port=port, username=user, password=pw, scheme=scheme)


def make_session_id() -> str:
    return secrets.token_hex(4)


def build_proxy_url(
    cfg: EvomiConfig,
    *,
    session: Optional[str] = None,
    country: str = "US",
    lifetime_min: int = 30,
) -> str:
    pw_parts = [cfg.password]
    if country:
        pw_parts.append(f"country-{country}")
    if session:
        pw_parts.append(f"session-{session}")
        pw_parts.append(f"lifetime-{lifetime_min}")
    pw = "_".join(pw_parts)
    return f"{cfg.scheme}://{quote(cfg.username, safe='_-')}:{quote(pw, safe='_-')}@{cfg.host}:{cfg.port}"


# ──────────────────────────────────────────────────────────────────────────
# Webshare (rotating)
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WebshareConfig:
    host: str
    port: int
    username: str
    password: str

    @classmethod
    def from_env(cls) -> "WebshareConfig":
        load_dotenv()
        host = os.getenv("WEBSHARE_HOST", "p.webshare.io")
        port = int(os.getenv("WEBSHARE_PORT", "80"))
        user = os.getenv("WEBSHARE_USERNAME") or ""
        pw = os.getenv("WEBSHARE_PASSWORD") or ""
        if not user or not pw:
            raise SystemExit("Set WEBSHARE_USERNAME and WEBSHARE_PASSWORD in .env")
        return cls(host=host, port=port, username=user, password=pw)

    def url(self) -> str:
        return f"http://{quote(self.username, safe='-_')}:{quote(self.password, safe='-_')}@{self.host}:{self.port}"


# ──────────────────────────────────────────────────────────────────────────
# Resolver — pick a backend by name
# ──────────────────────────────────────────────────────────────────────────


def resolve_proxy_url(backend: str = "evomi") -> str:
    """Return a proxy URL for the named backend.

    backend ∈ {"evomi", "webshare", "none"}
    """
    backend = (backend or "evomi").lower()
    if backend == "none":
        return ""
    if backend == "webshare":
        return WebshareConfig.from_env().url()
    if backend == "evomi":
        return build_proxy_url(EvomiConfig.from_env(), session=make_session_id())
    raise ValueError(f"unknown proxy backend: {backend}")


def split_proxy_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "server": f"{p.scheme}://{p.hostname}:{p.port}",
        "username": unquote(p.username or ""),
        "password": unquote(p.password or ""),
    }

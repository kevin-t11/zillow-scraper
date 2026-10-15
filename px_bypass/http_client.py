"""Property-detail HTTP transport with consistent Chrome connection settings."""

import asyncio

from curl_cffi.requests import AsyncSession


# Pin this together with curl_cffi in requirements.txt so deploys use the
# profile verified by the live endpoint checks, rather than a moving alias.
CHROME_PROFILE = "chrome150"


class PropertyClient(AsyncSession):
    """Keep one connection pool and server cookie jar per Evomi session."""

    def __init__(self, *, proxy: str | None, max_clients: int):
        super().__init__(
            proxy=proxy, max_clients=max_clients, impersonate=CHROME_PROFILE,
            verify=True, trust_env=False, allow_redirects=True, max_redirects=10,
            timeout=30, retry=0,
        )
        self._close_lock = asyncio.Lock()

    async def get(self, url: str, *, headers=None, **kwargs):
        # A Chrome User-Agent on HTTPX does not change its TLS/HTTP2 settings.
        # Let the transport supply matching browser headers; keep Zillow's
        # application headers (client-id, referer, etc.) supplied by the caller.
        headers = {
            key: value for key, value in (headers or {}).items()
            if key.lower() != "user-agent" and not key.lower().startswith("sec-ch-ua")
        }
        return await super().get(url, headers=headers, **kwargs)

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        # Slot retirement and pool shutdown can both close the same client.
        async with self._close_lock:
            if not self._closed:
                await super().close()

    async def aclose(self) -> None:
        # Match the pool's HTTPX lifecycle interface.
        await self.close()

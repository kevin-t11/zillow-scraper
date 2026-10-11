import ssl
import unittest
from unittest.mock import AsyncMock, patch

from px_bypass.cookie_pool import CookiePool
from px_bypass.proxy import EvomiConfig


class ProxyStream:
    """Simulate CONNECT and a closing origin response through real HTTPX/httpcore."""

    def __init__(self):
        self.responses = iter([
            b'HTTP/1.1 200 Connection established\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}',
        ])
        self.tls = []

    async def read(self, max_bytes, timeout=None):
        return next(self.responses, b'')

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((server_hostname, ssl_context))
        return self

    def get_extra_info(self, info):
        return None


class ProxyTlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnecting_sessions_reuse_verified_proxy_tls_without_sharing_cookies(self):
        pool = CookiePool(AsyncMock(), 2, evomi_cfg=EvomiConfig(
            'proxy.example.test', 1001, 'user', 'password', 'https'))
        clients = [pool._build_client(session) for session in ('first', 'second')]
        for client in clients:
            self.addAsyncCleanup(client.aclose)
        clients[0].cookies.set('zguid', 'first-session')
        self.assertNotIn('zguid', clients[1].cookies)
        streams = []

        async def connect(**kwargs):
            stream = ProxyStream()
            streams.append(stream)
            return stream

        # A missing explicit proxy TLS context would take this expensive path
        # on every connection. Exercise actual transport setup and reconnection.
        with patch('httpcore._backends.auto.AutoBackend.connect_tcp', side_effect=connect), patch(
            'httpcore._async.connection.default_ssl_context',
            side_effect=AssertionError('certificate store rebuilt during a request'),
        ):
            for client in (clients[0], clients[0], clients[1]):
                response = await client.get('https://listing.example.test/details')
                self.assertEqual(response.json(), {})

        self.assertEqual(len(streams), 3)
        proxy_contexts = []
        for stream in streams:
            self.assertEqual([host for host, _ in stream.tls],
                             ['proxy.example.test', 'listing.example.test'])
            proxy_context, origin_context = [context for _, context in stream.tls]
            self.assertIsNot(proxy_context, origin_context)
            for context in (proxy_context, origin_context):
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
            proxy_contexts.append(proxy_context)
        self.assertTrue(all(context is proxy_contexts[0] for context in proxy_contexts))

import asyncio
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from curl_cffi.requests.exceptions import Timeout

from px_bypass.http_client import PropertyClient
from px_bypass.scraper import QUERY_TEMPLATE_PATH, _attempt, _build_request


class PropertyTransportTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real libcurl requests through a local HTTP proxy, without Zillow."""

    async def asyncSetUp(self):
        self.requests = []
        self.handlers = set()
        self.server = await asyncio.start_server(self.respond, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.proxy = f'http://test-user:test-password@127.0.0.1:{port}'
        self.addAsyncCleanup(self.close_server)

    async def close_server(self):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            raw = (await reader.readuntil(b'\r\n\r\n')).decode()
            first, *lines = raw.split('\r\n')
            method, target, _ = first.split(' ')
            headers = dict(line.lower().split(': ', 1) for line in lines if ': ' in line)
            self.requests.append((method, target, headers))
            path = urlsplit(target).path
            if path == '/slow':
                await asyncio.sleep(1)
            status = '403 Forbidden' if path == '/blocked' else '200 OK'
            payload = json.dumps({'data': {'property': {'zpid': 51816594}}}).encode()
            writer.write((f'HTTP/1.1 {status}\r\nContent-Type: application/json\r\n'
                          f'Content-Length: {len(payload)}\r\nConnection: close\r\n'
                          'Set-Cookie: zguid=local-test; Path=/\r\n\r\n').encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    async def test_proxy_get_preserves_query_and_application_headers_and_isolates_cookies(self):
        clients = [PropertyClient(proxy=self.proxy, max_clients=2) for _ in range(2)]
        for client in clients:
            self.addAsyncCleanup(client.aclose)
        _, body, headers = _build_request('51816594', 'test', json.loads(QUERY_TEMPLATE_PATH.read_text()))
        for client in (clients[0], clients[0], clients[1]):
            status, content = await _attempt(client, 'http://listing.example.test/graphql/', body, headers, {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content)['data']['property']['zpid'], 51816594)
        for method, target, sent in self.requests:
            self.assertEqual(method, 'GET')
            self.assertEqual(urlsplit(target).hostname, 'listing.example.test')
            query = parse_qs(urlsplit(target).query)
            self.assertEqual(json.loads(query['variables'][0]), body['variables'])
            self.assertEqual(json.loads(query['extensions'][0]), body['extensions'])
            self.assertEqual(sent['client-id'], headers['client-id'])
            self.assertEqual(sent['referer'], headers['referer'])
            self.assertIn('chrome/150.', sent['user-agent'])
            self.assertIn('v="150"', sent['sec-ch-ua'])
            self.assertIn('proxy-authorization', sent)
        self.assertNotIn('cookie', self.requests[0][2])
        self.assertEqual(self.requests[1][2]['cookie'], 'zguid=local-test')
        self.assertNotIn('cookie', self.requests[2][2])
        await clients[0].aclose()
        self.assertTrue(clients[0].is_closed)
        self.assertFalse(clients[1].is_closed)

    async def test_rejections_and_timeouts_are_bounded_and_release_connection_capacity(self):
        async with PropertyClient(proxy=self.proxy, max_clients=1) as client:
            with self.assertRaises(Timeout):
                await client.get('http://listing.example.test/slow', timeout=0.05)
            response = await asyncio.wait_for(
                client.get('http://listing.example.test/blocked'), timeout=2)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(len(self.requests), 2)

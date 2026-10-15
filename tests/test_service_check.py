import unittest

import httpx

from px_bypass.check_service import SAMPLES, check_service


class ServiceCheckTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, change=None):
        def respond(request):
            path = request.url.path
            if path == '/health':
                payload = {'status': 'ok', 'live': 24, 'search_pool': {'live': 4}}
            elif path == '/v2/search/listings':
                payload = {'results': [{'zpid': str(i)} for i in range(5)]}
            else:
                payload = {'results': [{'zpid': z, 'status': 'success', 'photos': ['photo'],
                                       'description': 'description'} for z, _ in SAMPLES],
                           'summary': {'cached': 0, 'listing_cached': 0, 'resolve_cached': 0}}
            if change:
                change(path, payload)
            return httpx.Response(200, json=payload)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://test') as client:
            return await check_service(client)

    async def test_full_correct_uncached_responses_pass(self):
        self.assertTrue((await self.check())['passed'])

    async def test_http_200_with_wrong_id_is_not_success(self):
        def wrong(path, payload):
            if path.endswith('/addresses'):
                payload['results'][0]['zpid'] = '999'
        self.assertFalse((await self.check(wrong))['passed'])

    async def test_cached_response_does_not_prove_live_access(self):
        def cached(path, payload):
            if path.endswith('/urls'):
                payload['summary']['cached'] = 3
        self.assertFalse((await self.check(cached))['passed'])

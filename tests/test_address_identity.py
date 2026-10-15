import unittest
from unittest.mock import patch

import httpx

from app.scraper import autocomplete_address_matches, get_zpid_via_autocomplete, get_zpid_via_zillow_search
from px_bypass.resolver import resolve_one


class AddressIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_number_street_direction_suffix_and_unrequested_unit_are_rejected(self):
        wanted = '123 N Main St, City, CA 90240'
        for display in ['1230 N Main St, City, CA 90240', '123 N Other St, City, CA 90240',
                        '123 S Main St, City, CA 90240', '123 N Main Rd, City, CA 90240',
                        '123 N Main St #7, City, CA 90240', '123 N Main St, City, CA 902400']:
            with self.subTest(display=display):
                self.assertFalse(autocomplete_address_matches(wanted, display))
        self.assertTrue(autocomplete_address_matches(wanted, '123 North Main Street, City, CA 90240'))
        self.assertTrue(autocomplete_address_matches('123 N Main St City CA 90240',
                                                     '123 North Main Street, City, CA 90240'))

    async def test_directional_suffix_cannot_be_mistaken_for_a_city_separator(self):
        wanted = '7126 Marina Pacifica Dr, Long Beach, CA 90803'
        self.assertTrue(autocomplete_address_matches(wanted, '7126 Marina Pacifica Dr Long Beach, CA 90803'))
        self.assertFalse(autocomplete_address_matches(wanted, '7126 Marina Pacifica Dr N Long Beach, CA 90803'))
        self.assertFalse(autocomplete_address_matches(wanted, '7126 Marina Pacifica Dr S Long Beach, CA 90803'))

    async def test_exact_candidate_wins_over_prefix_and_nearby_suggestion(self):
        payload = {'results': [
            {'display': '1230 Main St, City, CA 90240', 'metaData': {'zpid': 10}},
            {'display': '123 Other St, City, CA 90240', 'metaData': {'zpid': 11}},
            {'display': '123 Main Street, City, CA 90240', 'metaData': {'zpid': 12}},
        ]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
            result = await get_zpid_via_autocomplete(client, '123 Main St, City, CA 90240', prefer_full_address=True)
        self.assertEqual(result[0], '12')

    async def test_ambiguous_ids_fail_closed_without_search_guessing(self):
        payload = {'results': [
            {'display': '123 Main St, City, CA 90240', 'metaData': {'zpid': zpid}} for zpid in [1, 2]
        ]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
            with patch('px_bypass.resolver.get_zpid_via_zillow_search', side_effect=AssertionError('ambiguous fallback')):
                result = await resolve_one(client, '123 Main St, City, CA 90240')
        self.assertIsNone(result)

    async def test_search_skips_nearby_home_and_requires_unique_matching_slug(self):
        body = '<a href="/homedetails/123-Other-St-City-CA-90240/1_zpid/">Wrong</a>'
        body += '<a href="/homedetails/123-Main-St-City-CA-90240/2_zpid/">Correct</a>'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
            result = await get_zpid_via_zillow_search(client, '123 Main St, City, CA 90240', require_address_match=True)
        self.assertEqual(result[0], '2')
        body += '<a href="/homedetails/123-Main-St-City-CA-90240/3_zpid/">Ambiguous</a>'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
            result = await get_zpid_via_zillow_search(client, '123 Main St, City, CA 90240', require_address_match=True)
        self.assertIsNone(result)

    async def test_search_redirect_does_not_accept_a_different_address(self):
        for slug, expected in [('123-Other-St-City-CA-90240', None), ('123-Main-St-City-CA-90240', '2')]:
            def respond(req):
                if '/homes/' in str(req.url):
                    return httpx.Response(302, headers={'location': f'/homedetails/{slug}/2_zpid/'})
                return httpx.Response(200, text='listing')
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await get_zpid_via_zillow_search(client, '123 Main St, City, CA 90240', require_address_match=True)
            self.assertEqual(result[0] if result else None, expected)

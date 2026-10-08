import unittest

from px_bypass.search import (
    SearchLocation,
    SearchListingPreview,
    build_filter_state,
    build_home_type_filter,
    build_search_payload,
    extract_search_listings,
    filter_listings_to_location,
)


class ZillowSearchTest(unittest.TestCase):
    def test_multifamily_filter_excludes_other_home_types(self):
        filters = build_home_type_filter(["multi_family"])

        self.assertNotIn("isMultiFamily", filters)
        self.assertEqual(filters["isSingleFamily"], {"value": False})
        self.assertEqual(filters["isCondo"], {"value": False})
        self.assertEqual(filters["isLotLand"], {"value": False})

    def test_for_sale_filter_defaults_to_all_active_listing_sources(self):
        filters = build_filter_state(status="for_sale", home_types=["multi_family"])

        self.assertEqual(filters["isForSaleByAgent"], {"value": True})
        self.assertEqual(filters["isForSaleByOwner"], {"value": True})
        self.assertEqual(filters["isNewConstruction"], {"value": True})
        self.assertEqual(filters["isComingSoon"], {"value": True})
        self.assertEqual(filters["isAuction"], {"value": False})
        self.assertEqual(filters["isForSaleForeclosure"], {"value": False})

    def test_payload_includes_city_region_selection(self):
        location = SearchLocation(
            query="Naperville, IL",
            display="Naperville, IL",
            region_id=39931,
            region_type="city",
            latitude=41.75,
            longitude=-88.16,
        )

        payload = build_search_payload(
            location=location,
            bounds={"north": 42, "south": 41, "east": -88, "west": -89},
            status="for_sale",
            home_types=["multi_family"],
        )

        state = payload["searchQueryState"]
        self.assertEqual(state["usersSearchTerm"], "Naperville, IL")
        self.assertEqual(state["regionSelection"], [{"regionId": 39931, "regionType": 6}])
        self.assertEqual(payload["wants"], {"cat1": ["listResults", "mapResults"]})

    def test_extract_search_listings_dedupes_and_normalizes_cards(self):
        payload = {
            "cat1": {
                "searchResults": {
                    "totalResultCount": 2,
                    "listResults": [
                        {
                            "zpid": 123,
                            "detailUrl": "/homedetails/123-Main-St/123_zpid/",
                            "addressStreet": "123 Main St",
                            "addressCity": "Naperville",
                            "addressState": "IL",
                            "addressZipcode": "60540",
                            "price": "$750,000",
                            "beds": 6,
                            "baths": 4,
                            "area": 3200,
                            "imgSrc": "https://photos.example/123.jpg",
                            "hdpData": {"homeInfo": {"homeType": "MULTI_FAMILY"}},
                        }
                    ],
                    "mapResults": [
                        {"zpid": 123, "detailUrl": "/homedetails/dupe/123_zpid/"},
                        {
                            "zpid": "456",
                            "detailUrl": "https://www.zillow.com/homedetails/456_zpid/",
                            "address": {
                                "streetAddress": "456 Center Ave",
                                "city": "Naperville",
                                "state": "IL",
                                "zipcode": "60563",
                            },
                            "unformattedPrice": 500000,
                            "miniCardPhotos": [{"url": "https://photos.example/456.jpg"}],
                            "homeType": "MULTI_FAMILY",
                        },
                    ],
                }
            }
        }

        listings, total = extract_search_listings(payload)

        self.assertEqual(total, 2)
        self.assertEqual([item.zpid for item in listings], ["123", "456"])
        self.assertEqual(listings[0].url, "https://www.zillow.com/homedetails/123-Main-St/123_zpid/")
        self.assertEqual(listings[0].full_address, "123 Main St, Naperville, IL 60540")
        self.assertEqual(listings[0].image, "https://photos.example/123.jpg")
        self.assertEqual(listings[1].price, 500000)
        self.assertEqual(listings[1].image, "https://photos.example/456.jpg")

    def test_strict_city_filter_removes_nearby_city_results(self):
        location = SearchLocation(
            query="Naperville, IL",
            display="Naperville, IL",
            region_id=39931,
            region_type="city",
            city="Naperville",
            state="IL",
            latitude=41.75,
            longitude=-88.16,
        )
        listings = [
            SearchListingPreview(zpid="1", url="https://example.com/1", city="Naperville", state="IL"),
            SearchListingPreview(zpid="2", url="https://example.com/2", city="Aurora", state="IL"),
        ]

        kept, discarded = filter_listings_to_location(listings, location)

        self.assertEqual([item.zpid for item in kept], ["1"])
        self.assertEqual(discarded, 1)


if __name__ == "__main__":
    unittest.main()

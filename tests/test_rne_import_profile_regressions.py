from __future__ import annotations

import unittest

from local_osm.import_profile import DISCOVERY_KEYS, is_relevant, is_relevant_items


class H145ImportProfileRegressionTests(unittest.TestCase):
    """Installed H145 mission selectors that must survive import pruning."""

    def test_charging_station_selector_is_retained(self) -> None:
        # HPG Action Pack selector: [amenity=charging_station]
        tags = {"amenity": "charging_station"}
        self.assertTrue(is_relevant(tags))
        self.assertTrue(is_relevant_items(list(tags.items())))
        self.assertIn("amenity", DISCOVERY_KEYS)

    def test_golf_green_selector_is_retained(self) -> None:
        # HPG Action Pack selector: [golf=green]
        tags = {"golf": "green"}
        self.assertTrue(is_relevant(tags))
        self.assertTrue(is_relevant_items(list(tags.items())))
        self.assertIn("golf", DISCOVERY_KEYS)

    def test_iata_existence_selector_is_retained_for_any_value(self) -> None:
        # HPG/third-party offshore missions use way[iata](bbox).
        self.assertTrue(is_relevant({"iata": "EFHK"}))
        self.assertTrue(is_relevant({"iata": "JFK"}))
        self.assertTrue(is_relevant_items([("iata", "LHR")]))
        self.assertIn("iata", DISCOVERY_KEYS)

    def test_profile_remains_exact_for_other_amenities(self) -> None:
        self.assertFalse(is_relevant({"amenity": "bench"}))

    def test_profile_remains_exact_for_other_golf_values(self) -> None:
        self.assertFalse(is_relevant({"golf": "tee"}))


if __name__ == "__main__":
    unittest.main()

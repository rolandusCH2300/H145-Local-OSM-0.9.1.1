from __future__ import annotations

import unittest

from local_osm.gui.regions import (
    conflicting_region_ids,
    direct_children,
    region_selections_conflict,
    selectable_parent_records,
)


class RegionHierarchyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            {"id": "europe", "name": "Europe", "parent": None, "pbf": "europe"},
            {"id": "finland", "name": "Finland", "parent": "europe", "pbf": "finland"},
            {"id": "germany", "name": "Germany", "parent": "europe", "pbf": "germany"},
            {"id": "bavaria", "name": "Bavaria", "parent": "germany", "pbf": "bavaria"},
            {"id": "asia", "name": "Asia", "parent": None, "pbf": "asia"},
            {"id": "japan", "name": "Japan", "parent": "asia", "pbf": "japan"},
        ]

    def test_parent_selection_excludes_all_descendants(self):
        self.assertEqual(
            conflicting_region_ids(self.records, {"europe"}),
            {"europe", "finland", "germany", "bavaria"},
        )

    def test_child_selection_excludes_ancestors_and_descendants(self):
        self.assertEqual(
            conflicting_region_ids(self.records, {"germany"}),
            {"europe", "germany", "bavaria"},
        )

    def test_unrelated_regions_remain_available(self):
        conflicts = conflicting_region_ids(self.records, {"finland"})
        self.assertIn("europe", conflicts)
        self.assertIn("finland", conflicts)
        self.assertNotIn("germany", conflicts)
        self.assertNotIn("asia", conflicts)
        self.assertNotIn("japan", conflicts)

    def test_parent_dropdown_includes_top_level_and_nested_parents(self):
        self.assertEqual(
            [record["id"] for record in selectable_parent_records(self.records)],
            ["asia", "europe", "germany"],
        )
        self.assertEqual(
            [record["id"] for record in direct_children(self.records, "europe")],
            ["finland", "germany"],
        )

    def test_siblings_are_compatible_but_parent_child_are_not(self):
        self.assertFalse(
            region_selections_conflict(self.records, ["finland", "germany"])
        )
        self.assertTrue(
            region_selections_conflict(self.records, ["europe", "finland"])
        )
        self.assertTrue(
            region_selections_conflict(self.records, ["germany", "bavaria"])
        )


if __name__ == "__main__":
    unittest.main()

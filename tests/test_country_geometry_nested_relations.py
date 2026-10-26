from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_osm.country_geometry import recover_country_geometry
from local_osm.db import (
    OSMObject,
    connect,
    encode_way_geometry_v5,
    insert_object,
    insert_relation_member_rows,
    insert_way_geometry_v5_rows,
)
from local_osm.query import execute_overpass_subset


class CountryGeometryNestedRelationTests(unittest.TestCase):
    """Regression for admin-boundary geometry carried by nested outer relations."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / "nested-country.sqlite3")

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def _add_way(self, way_id: int, nodes: list[tuple[int, float, float]]) -> None:
        lats = [lat for _nid, lat, _lon in nodes]
        lons = [lon for _nid, _lat, lon in nodes]
        insert_object(
            self.con,
            OSMObject(
                "way",
                way_id,
                (min(lats) + max(lats)) / 2.0,
                (min(lons) + max(lons)) / 2.0,
                min(lats),
                max(lats),
                min(lons),
                max(lons),
                {},
                False,
            ),
        )
        node_ids, coordinates = encode_way_geometry_v5(nodes)
        insert_way_geometry_v5_rows(self.con, [(way_id, node_ids, coordinates)])

    def _add_square(
        self,
        first_way: int,
        first_node: int,
        lat0: float,
        lon0: float,
        size: float,
    ) -> list[int]:
        n1, n2, n3, n4 = range(first_node, first_node + 4)
        ways = list(range(first_way, first_way + 4))
        self._add_way(ways[0], [(n1, lat0, lon0), (n2, lat0, lon0 + size)])
        self._add_way(ways[1], [(n3, lat0 + size, lon0 + size), (n2, lat0, lon0 + size)])
        self._add_way(ways[2], [(n3, lat0 + size, lon0 + size), (n4, lat0 + size, lon0)])
        self._add_way(ways[3], [(n1, lat0, lon0), (n4, lat0 + size, lon0)])
        return ways

    def _is_in(self, relation_id: int, lat: float, lon: float) -> bool:
        query = (
            "[out:json];"
            f"is_in({lat},{lon})->.a;"
            "rel(pivot.a)[boundary=administrative][admin_level=2];"
            "out tags;"
        )
        return relation_id in [
            element["id"] for element in execute_overpass_subset(self.con, query)
        ]

    def test_parent_country_recovers_geometry_from_nested_outer_relation(self) -> None:
        mainland_ways = self._add_square(100, 1, 0.0, 0.0, 10.0)

        # Synthetic child relation carries one exact outer boundary component.
        insert_object(
            self.con,
            OSMObject(
                "relation", 9001, 5.0, 5.0, 0.0, 10.0, 0.0, 10.0,
                {"type": "boundary", "boundary": "administrative", "admin_level": "3"},
                True,
            ),
        )
        insert_relation_member_rows(
            self.con,
            9001,
            [("way", way_id, "outer") for way_id in mainland_ways],
        )

        # Parent also has an incomplete remote outer component. Recovery must
        # keep the exact nested closed ring and discard only the broken component.
        self._add_way(200, [(20, 20.0, 20.0), (21, 20.0, 25.0)])
        self._add_way(201, [(21, 20.0, 25.0), (22, 25.0, 25.0)])
        insert_object(
            self.con,
            OSMObject(
                "relation", 9002, 12.5, 12.5, 0.0, 25.0, 0.0, 25.0,
                {
                    "type": "boundary",
                    "boundary": "administrative",
                    "admin_level": "2",
                    "ISO3166-1:alpha2": "ZZ",
                    "name": "Synthetic Country",
                },
                True,
            ),
        )
        insert_relation_member_rows(
            self.con,
            9002,
            [
                ("relation", 9001, "outer"),
                ("way", 200, "outer"),
                ("way", 201, "outer"),
                ("way", 202, "outer"),
            ],
        )
        self.con.commit()

        result = recover_country_geometry(self.con, 9002)

        self.assertEqual(result.status, "recovered")
        self.assertGreaterEqual(result.outer_rings, 1)
        self.assertEqual(result.missing_way_members, 1)
        self.assertTrue(
            self._is_in(9002, 5.0, 5.0),
            "closed component inside nested outer relation must be contained by parent country",
        )
        self.assertFalse(self._is_in(9002, 22.0, 22.0))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_osm.country_geometry import (
    recover_country_geometry,
    recover_selected_country_geometries,
)
from local_osm.db import (
    OSMObject,
    connect,
    encode_way_geometry_v5,
    get_area_geometry,
    insert_object,
    insert_relation_member_rows,
    insert_way_geometry_v5_rows,
)
from local_osm.query import execute_overpass_subset


class CountryGeometryFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / "country-fallback.sqlite3")

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

    def _add_country(
        self,
        relation_id: int,
        iso2: str,
        members: list[tuple[str, int, str]],
        *,
        bbox: tuple[float, float, float, float] = (0.0, 30.0, 0.0, 30.0),
    ) -> None:
        min_lat, max_lat, min_lon, max_lon = bbox
        insert_object(
            self.con,
            OSMObject(
                "relation",
                relation_id,
                (min_lat + max_lat) / 2.0,
                (min_lon + max_lon) / 2.0,
                min_lat,
                max_lat,
                min_lon,
                max_lon,
                {
                    "type": "boundary",
                    "boundary": "administrative",
                    "admin_level": "2",
                    "ISO3166-1:alpha2": iso2,
                    "name": f"Country {iso2}",
                },
                True,
            ),
        )
        insert_relation_member_rows(self.con, relation_id, members)
        self.con.commit()

    def _add_square(self, first_way: int, first_node: int, lat0: float, lon0: float, size: float) -> list[int]:
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
        return relation_id in [e["id"] for e in execute_overpass_subset(self.con, query)]

    def test_recovers_closed_component_and_discards_component_broken_by_missing_way(self) -> None:
        mainland = self._add_square(100, 1, 0.0, 0.0, 10.0)
        self._add_way(200, [(20, 20.0, 20.0), (21, 20.0, 25.0)])
        self._add_way(201, [(21, 20.0, 25.0), (22, 25.0, 25.0)])
        missing_way = 202
        members = [("way", wid, "outer") for wid in mainland]
        members += [("way", 200, "outer"), ("way", 201, "outer"), ("way", missing_way, "outer")]
        self._add_country(500, "NO", members)

        result = recover_country_geometry(self.con, 500)

        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.outer_rings, 1)
        self.assertEqual(result.missing_way_members, 1)
        self.assertEqual(result.skipped_open_outer_components, 1)
        geom = json.loads(get_area_geometry(self.con, self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=500"
        ).fetchone()[0]))
        self.assertEqual(geom["type"], "Polygon")
        self.assertTrue(self._is_in(500, 5.0, 5.0))
        self.assertFalse(self._is_in(500, 22.0, 22.0))

    def test_does_not_create_geometry_when_only_available_outer_component_is_open(self) -> None:
        self._add_way(300, [(30, 0.0, 0.0), (31, 0.0, 10.0)])
        self._add_way(301, [(31, 0.0, 10.0), (32, 10.0, 10.0)])
        self._add_country(
            600,
            "SE",
            [("way", 300, "outer"), ("way", 301, "outer"), ("way", 302, "outer")],
        )

        result = recover_country_geometry(self.con, 600)

        self.assertEqual(result.status, "no_closed_outer")
        self.assertIsNone(get_area_geometry(self.con, self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=600"
        ).fetchone()[0]))

    def test_preserves_exact_inner_hole(self) -> None:
        outer = self._add_square(400, 40, 0.0, 0.0, 10.0)
        inner = self._add_square(410, 50, 4.0, 4.0, 2.0)
        members = [("way", wid, "outer") for wid in outer]
        members += [("way", wid, "inner") for wid in inner]
        self._add_country(700, "NO", members, bbox=(0.0, 10.0, 0.0, 10.0))

        result = recover_country_geometry(self.con, 700)

        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.outer_rings, 1)
        self.assertEqual(result.inner_rings, 1)
        self.assertTrue(self._is_in(700, 2.0, 2.0))
        self.assertFalse(self._is_in(700, 5.0, 5.0))

    def test_selected_iso_codes_gate_fallback_to_requested_countries(self) -> None:
        norway = self._add_square(500, 60, 0.0, 0.0, 5.0)
        sweden = self._add_square(510, 70, 10.0, 10.0, 5.0)
        self._add_country(800, "NO", [("way", wid, "outer") for wid in norway])
        self._add_country(801, "SE", [("way", wid, "outer") for wid in sweden])

        results = recover_selected_country_geometries(self.con, {"NO"})

        self.assertEqual([result.relation_id for result in results], [800])
        norway_pk = self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=800"
        ).fetchone()[0]
        sweden_pk = self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=801"
        ).fetchone()[0]
        self.assertIsNotNone(get_area_geometry(self.con, norway_pk))
        self.assertIsNone(get_area_geometry(self.con, sweden_pk))


if __name__ == "__main__":
    unittest.main()

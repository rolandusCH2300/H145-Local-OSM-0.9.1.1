import json
import tempfile
import unittest
from pathlib import Path

from local_osm.db import OSMObject, connect, insert_object, set_area_geometry
from local_osm.query import execute_overpass_subset


class CountryIsInRegressionTests(unittest.TestCase):
    """Regression tests for exact country containment in Overpass is_in()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / "country_is_in_regression.sqlite3")

        insert_object(
            self.con,
            OSMObject(
                "relation",
                1,
                5.0,
                5.0,
                0.0,
                10.0,
                0.0,
                10.0,
                {
                    "type": "boundary",
                    "boundary": "administrative",
                    "admin_level": "2",
                    "name": "Exactland",
                },
                True,
            ),
        )
        set_area_geometry(
            self.con,
            "relation",
            1,
            json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]],
                }
            ),
        )

        # Deliberately bbox-only: this relation overlaps the query point in the
        # RTree but has no exact polygon geometry and therefore must not match.
        insert_object(
            self.con,
            OSMObject(
                "relation",
                2,
                5.0,
                5.0,
                0.0,
                10.0,
                0.0,
                10.0,
                {
                    "type": "boundary",
                    "boundary": "administrative",
                    "admin_level": "2",
                    "name": "BboxOnlyland",
                },
                True,
            ),
        )
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_bbox_only_country_candidate_is_not_containment(self):
        query = (
            "[out:json];"
            "is_in(2.0,2.0)->.a;"
            "rel(pivot.a)[boundary=administrative][admin_level=2];"
            "out tags;"
        )
        out = execute_overpass_subset(self.con, query)

        self.assertEqual(
            [element["id"] for element in out],
            [1],
            "is_in() must require exact area geometry; bbox-only country candidates must be rejected",
        )


if __name__ == "__main__":
    unittest.main()

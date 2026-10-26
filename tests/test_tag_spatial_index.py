from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_osm.db import OSMObject, connect, insert_object, seed_demo
from local_osm.query import QueryProfile, execute_overpass_subset
from local_osm.tag_spatial_index import (
    FEATURE_BY_FILTER,
    FEATURES,
    TAG_SPATIAL_INDEX_VERSION,
    build_tag_spatial_index,
    tag_spatial_index_ready,
)


SELECTIVE_FILTERS = (
    ("water", "lake"),
    ("landuse", "forest"),
    ("highway", "cycleway"),
    ("natural", "peak"),
    ("crossing", "uncontrolled"),
    ("highway", "crossing"),
    ("building", "apartments"),
    ("amenity", "parking"),
)


class TagSpatialIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "tag-spatial.sqlite3"
        self.con = connect(self.db)
        seed_demo(self.con)

        # Add selective predicates not already present in seed_demo.
        insert_object(
            self.con,
            OSMObject(
                "way",
                970000001,
                60.4540,
                22.2700,
                60.4535,
                60.4545,
                22.2695,
                22.2705,
                {"landuse": "forest"},
                True,
            ),
        )
        insert_object(
            self.con,
            OSMObject(
                "way",
                970000002,
                60.4550,
                22.2710,
                60.4545,
                60.4555,
                22.2705,
                22.2715,
                {"highway": "cycleway"},
                False,
            ),
        )
        insert_object(
            self.con,
            OSMObject(
                "way",
                970000003,
                60.4560,
                22.2720,
                60.4555,
                60.4565,
                22.2715,
                22.2725,
                {"amenity": "parking"},
                True,
            ),
        )
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_registry_is_complete_and_versioned(self) -> None:
        self.assertEqual(TAG_SPATIAL_INDEX_VERSION, 3)
        self.assertEqual(len(FEATURES), 15)
        for key, value in SELECTIVE_FILTERS:
            feature = FEATURE_BY_FILTER.get((key, value))
            self.assertIsNotNone(feature, f"missing selective feature {key}={value}")

    def test_v2_bundle_is_not_mistaken_for_complete_v3_bundle(self) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) "
            "VALUES('tag_spatial_index_version','2')"
        )
        self.con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) "
            "VALUES('tag_spatial_index_features','14')"
        )
        self.con.commit()
        self.assertFalse(tag_spatial_index_ready(self.con))

    def test_selective_features_build_and_are_used_by_spatial_first_queries(self) -> None:
        total, counts, _elapsed = build_tag_spatial_index(self.con, verbose=False)
        self.assertGreater(total, 0)
        self.assertTrue(tag_spatial_index_ready(self.con))

        # Force spatial-first to verify that each registered predicate uses its RTree.
        with (
            patch("local_osm.query.ADAPTIVE_SMALL_TAG_LIMIT", 0),
            patch("local_osm.query._bounded_exact_tag_count", return_value=1000),
            patch("local_osm.query._bounded_spatial_count", return_value=1),
        ):
            for key, value in SELECTIVE_FILTERS:
                feature = FEATURE_BY_FILTER[(key, value)]
                self.assertGreater(counts[feature.name], 0, f"empty selective feature {key}={value}")

                profile = QueryProfile()
                query = (
                    f"[out:json];nwr(60.40,22.20,60.50,22.35)"
                    f"[{key}={value}];out center;"
                )
                result = execute_overpass_subset(self.con, query, profile=profile)

                self.assertTrue(result, f"query returned no fixture rows for {key}={value}")
                self.assertEqual(
                    profile.adaptive_spatial_first_calls,
                    1,
                    f"planner was not forced spatial-first for {key}={value}",
                )
                self.assertEqual(
                    profile.tag_spatial_index_calls,
                    1,
                    f"dedicated RTree was not used for {key}={value}",
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_osm.import_profile import ANY_VALUE_KEYS, EXACT_VALUES
from local_osm.mission_audit import audit_occurrences, scan_mission_queries


class MissionAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_json(self, relative: str, payload) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_scans_every_hpgmission_package_and_ignores_other_json(self) -> None:
        self._write_json(
            "pkg-a/hpgmission/one.json",
            {
                "query": "[out:json];way[iata]({{bbox}});out center;",
                "other": "DB:H_HOSPITAL",
            },
        )
        self._write_json(
            "pkg-b/hpgmission/two.json",
            {
                "nested": [
                    {"QUERY": "[out:json];way({{bbox}})[amenity=school][name];out center;"}
                ]
            },
        )
        self._write_json(
            "pkg-c/not-hpgmission/ignored.json",
            {"query": "[out:json];way({{bbox}})[craft=carpenter];out center;"},
        )

        scan = scan_mission_queries(self.root)

        self.assertEqual(scan.files_scanned, 2)
        self.assertEqual(scan.files_with_osm, 2)
        self.assertEqual(len(scan.occurrences), 2)
        self.assertEqual({item.package for item in scan.occurrences}, {"pkg-a", "pkg-b"})
        self.assertEqual(scan.errors, [])

    def test_detects_iata_as_missing_any_value_requirement(self) -> None:
        self._write_json(
            "pkg-offshore/hpgmission/offshore.json",
            {"query": "[out:json];way[iata]({{bbox}});out center;"},
        )
        scan = scan_mission_queries(self.root)

        result = audit_occurrences(
            scan,
            any_value_keys={"highway", "railway"},
            exact_values=EXACT_VALUES,
        )

        self.assertEqual(result.selector_branches, 1)
        self.assertEqual(result.covered_branches, 0)
        self.assertEqual(result.uncovered_branches, 1)
        self.assertEqual(len(result.missing_requirements), 1)
        missing = result.missing_requirements[0]
        self.assertEqual((missing.mode, missing.key), ("any", "iata"))
        self.assertEqual(missing.values, ())
        self.assertEqual(len(missing.sources), 1)

    def test_current_profile_covers_iata_and_action_pack_selectors(self) -> None:
        queries = [
            "[out:json];way[iata]({{bbox}});out center;",
            "[out:json];(way({{bbox}})[amenity=\"charging_station\"];node({{bbox}})[amenity=\"charging_station\"];);(._;>;);out;",
            "[out:json];way({{bbox}})[golf=\"green\"];out center;",
        ]
        self._write_json(
            "hpg-airbus-h145-ap/hpgmission/mission.json",
            {"queries": queries},
        )
        scan = scan_mission_queries(self.root)

        result = audit_occurrences(
            scan,
            any_value_keys=ANY_VALUE_KEYS,
            exact_values=EXACT_VALUES,
        )

        self.assertEqual(result.selector_branches, 4)
        self.assertEqual(result.covered_branches, 4)
        self.assertEqual(result.uncovered_branches, 0)
        self.assertEqual(result.missing_requirements, [])

    def test_secondary_filters_do_not_force_broad_retention(self) -> None:
        self._write_json(
            "pkg/hpgmission/mission.json",
            {
                "queries": [
                    "[out:json];way({{bbox}})[amenity=school][name];out center;",
                    "[out:json];nwr({{bbox}})[leisure=pitch][sport=soccer][surface!=artificial_turf][indoor!=yes];out center;",
                    "[out:json];nwr({{bbox}})[building~\"^(apartments|house)$\"];out center;",
                ]
            },
        )
        scan = scan_mission_queries(self.root)

        result = audit_occurrences(
            scan,
            any_value_keys=ANY_VALUE_KEYS,
            exact_values=EXACT_VALUES,
        )

        self.assertEqual(result.selector_branches, 3)
        self.assertEqual(result.uncovered_branches, 0)
        self.assertNotIn("name", result.required_any_keys)
        self.assertNotIn("surface", result.required_any_keys)
        self.assertNotIn("indoor", result.required_any_keys)


if __name__ == "__main__":
    unittest.main()

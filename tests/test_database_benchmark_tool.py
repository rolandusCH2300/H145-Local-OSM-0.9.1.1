from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_osm.database_benchmark import (
    BenchmarkReport,
    BenchmarkResult,
    benchmark_database,
    choose_benchmark_anchor,
    classify_query,
)
from local_osm.database_benchmark_compare import evaluate_reports
from local_osm.db import connect, seed_demo


class DatabaseBenchmarkToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite3"
        con = connect(self.db)
        seed_demo(con)
        con.commit()
        con.close()

        self.packages = self.root / "Community"
        mission = self.packages / "pkg" / "hpgmission" / "mission.json"
        mission.parent.mkdir(parents=True, exist_ok=True)
        mission.write_text(
            json.dumps(
                {
                    "queries": [
                        "[out:json];nwr({{bbox}})[amenity=hospital];out center;",
                        "[out:json];way({{bbox}})[highway=primary];(._;>;);out;",
                        "[out:json];nwr({{bbox}})[amenity=hospital];out center;",
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _synthetic_report(*, average_ms: float, total_ms: float, result_count: int = 4) -> BenchmarkReport:
        result = BenchmarkResult(
            name="mission_001",
            query="[out:json];nwr(around:5000,60,24)[amenity=hospital];out center;",
            sources=("pkg: mission.json",),
            features=("around", "exact", "nwr"),
            executions=7,
            samples_ms=[average_ms] * 7,
            average_ms=average_ms,
            median_ms=average_ms,
            min_ms=average_ms,
            max_ms=average_ms,
            result_count=result_count,
            profile_average={},
            error=None,
        )
        return BenchmarkReport(
            database="synthetic.sqlite3",
            packages_root="Community",
            generated_at="2026-09-03T00:00:00+00:00",
            mission_profile_hash="a" * 64,
            mission_query_occurrences=1,
            unique_mission_queries=1,
            benchmark_cases=1,
            repeat=7,
            radius_m=5000.0,
            lat=60.0,
            lon=24.0,
            anchor_source="explicit",
            total_executions=7,
            total_elapsed_ms=total_ms,
            results=[result],
            database_metadata={},
        )

    def test_classify_query_reports_important_execution_shapes(self) -> None:
        features = classify_query(
            "[out:json];way(around:5000,60,22)[highway~\"primary|secondary\"];(._;>;);out;"
        )
        self.assertIn("around", features)
        self.assertIn("regex", features)
        self.assertIn("recurse", features)
        self.assertIn("way", features)

    def test_auto_anchor_prefers_real_hospital_or_helipad_from_database(self) -> None:
        lat, lon, source = choose_benchmark_anchor(self.db)
        self.assertAlmostEqual(lat, 60.4518, places=4)
        self.assertAlmostEqual(lon, 22.2666, places=4)
        self.assertIn(source, {"amenity=hospital", "aeroway=helipad"})

    def test_benchmark_runs_each_unique_template_exactly_three_times(self) -> None:
        report = benchmark_database(
            self.db,
            self.packages,
            repeat=3,
            radius_m=5000.0,
            lat=60.4518,
            lon=22.2666,
            include_is_in=False,
        )

        self.assertEqual(report.repeat, 3)
        self.assertEqual(report.unique_mission_queries, 2)
        self.assertEqual(len(report.results), 2)
        self.assertTrue(report.ok)
        for result in report.results:
            self.assertEqual(len(result.samples_ms), 3)
            self.assertGreaterEqual(result.average_ms, 0.0)
            self.assertEqual(result.executions, 3)
            self.assertIsInstance(result.profile_average, dict)
            self.assertIn("coverage_prepare_ms", result.profile_average)
            self.assertIn("coverage_filter_ms", result.profile_average)
            self.assertGreaterEqual(result.profile_average["unprofiled_ms"], 0.0)

    def test_paired_gate_accepts_matching_results_within_ten_percent(self) -> None:
        old = self._synthetic_report(average_ms=100.0, total_ms=1000.0)
        new = self._synthetic_report(average_ms=108.0, total_ms=1080.0)
        checks = evaluate_reports(old, new, max_regression_pct=10.0)
        self.assertTrue(all(check.ok for check in checks))

    def test_paired_gate_rejects_result_count_mismatch(self) -> None:
        old = self._synthetic_report(average_ms=100.0, total_ms=1000.0, result_count=4)
        new = self._synthetic_report(average_ms=90.0, total_ms=900.0, result_count=5)
        checks = evaluate_reports(old, new, max_regression_pct=10.0)
        result_check = next(check for check in checks if check.name == "query_result_counts")
        self.assertFalse(result_check.ok)

    def test_paired_gate_rejects_material_p95_and_total_regression(self) -> None:
        old = self._synthetic_report(average_ms=100.0, total_ms=1000.0)
        new = self._synthetic_report(average_ms=112.0, total_ms=1120.0)
        checks = evaluate_reports(old, new, max_regression_pct=10.0)
        by_name = {check.name: check for check in checks}
        self.assertFalse(by_name["p95_query_average_regression"].ok)
        self.assertFalse(by_name["total_elapsed_regression"].ok)


if __name__ == "__main__":
    unittest.main()

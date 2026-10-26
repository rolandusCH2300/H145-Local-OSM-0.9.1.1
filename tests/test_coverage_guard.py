from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from local_osm.coverage import (
    CoverageGuard,
    CoverageMiss,
    build_geofabrik_coverage_payload,
    install_coverage_metadata,
)
from local_osm.db import OSMObject, connect, insert_object, seed_demo
from local_osm.query import QueryProfile, execute_overpass_subset
from local_osm.server import State, make_handler


API_HEADER = "[H145 Local OSM] API\n"


def _square(west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def _feature(region_id: str, name: str, iso2: list[str], geometry: dict) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "id": region_id,
            "name": name,
            "parent": "europe",
            "iso3166-1:alpha2": iso2,
            "urls": {"pbf": f"https://example.invalid/{region_id}.osm.pbf"},
        },
        "geometry": geometry,
    }


def _index() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            _feature("monaco", "Monaco", ["MC"], _square(7.40, 43.72, 7.46, 43.77)),
            _feature("france", "France", ["FR"], _square(7.46, 43.50, 8.20, 44.50)),
        ],
    }


def _guard() -> CoverageGuard:
    encoded = build_geofabrik_coverage_payload(_index(), ["monaco"])
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "coverage.sqlite3"
        con = connect(db)
        con.close()
        install_coverage_metadata(db, encoded)
        con = connect(db)
        try:
            return CoverageGuard.from_connection(con)
        finally:
            con.close()


class CoverageGuardUnitTests(unittest.TestCase):
    def test_is_in_inside_selected_coverage_is_allowed(self):
        guard = _guard()
        result = guard.check_query(
            "[out:json];is_in(43.74,7.42)->.a;"
            "rel(pivot.a)[boundary=administrative][admin_level=2];out tags;"
        )
        self.assertIsNone(result.diagnostic)

    def test_is_in_in_unloaded_country_is_rejected(self):
        guard = _guard()
        with self.assertRaises(CoverageMiss) as caught:
            guard.check_query(
                "[out:json];is_in(43.80,7.50)->.a;"
                "rel(pivot.a)[boundary=administrative][admin_level=2];out tags;"
            )
        self.assertEqual(caught.exception.diagnostic["selector"], "is_in")
        self.assertEqual(caught.exception.diagnostic["unloaded_region"]["id"], "france")

    def test_around_anchor_in_unloaded_country_is_allowed_with_filter_diagnostic(self):
        guard = _guard()
        for radius in (2000, 32000, 150000):
            with self.subTest(radius=radius):
                result = guard.check_query(
                    f"[out:json];nwr(around:{radius},43.80,7.50)"
                    "[building=detached];out center;"
                )
                self.assertIsNotNone(result.diagnostic)
                self.assertEqual(
                    result.diagnostic["classification"],
                    "around_anchor_outside_loaded_coverage",
                )
                self.assertEqual(
                    result.diagnostic["action"],
                    "allowed_with_root_filter",
                )

    def test_around_anchor_with_no_country_match_is_allowed_for_offshore_behavior(self):
        guard = _guard()
        result = guard.check_query(
            "[out:json];nwr(around:2000,43.80,9.00)[natural=water];out center;"
        )
        self.assertIsNone(result.diagnostic)

    def test_bbox_entirely_inside_loaded_coverage_is_allowed(self):
        guard = _guard()
        result = guard.check_query(
            "[out:json];nwr(43.73,7.41,43.75,7.44)[building=yes];out center;"
        )
        self.assertIsNone(result.diagnostic)

    def test_bbox_entirely_inside_unloaded_country_is_allowed_with_filter_diagnostic(self):
        guard = _guard()
        result = guard.check_query(
            "[out:json];nwr(43.80,7.50,43.82,7.54)[building=yes];out center;"
        )
        self.assertIsNotNone(result.diagnostic)
        self.assertEqual(
            result.diagnostic["classification"],
            "bbox_outside_loaded_coverage",
        )
        self.assertEqual(result.diagnostic["action"], "allowed_with_root_filter")

    def test_bbox_loaded_unloaded_overlap_is_allowed_with_debug_diagnostic(self):
        guard = _guard()
        result = guard.check_query(
            "[out:json];nwr(43.73,7.44,43.80,7.50)[building=yes];out center;"
        )
        self.assertIsNotNone(result.diagnostic)
        self.assertEqual(
            result.diagnostic["classification"],
            "bbox_loaded_unloaded_overlap",
        )
        self.assertEqual(result.diagnostic["action"], "allowed_with_root_filter")
        self.assertEqual(result.diagnostic["loaded_regions"], ["monaco"])
        self.assertEqual(result.diagnostic["unloaded_regions"][0]["id"], "france")


class CoverageFilteringTests(unittest.TestCase):
    def _database(self):
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "filter.sqlite3"
        con = connect(db)
        insert_object(
            con,
            OSMObject(
                "node",
                101,
                43.740,
                7.420,
                43.740,
                43.740,
                7.420,
                7.420,
                {"amenity": "parking"},
                False,
            ),
        )
        insert_object(
            con,
            OSMObject(
                "node",
                202,
                43.800,
                7.500,
                43.800,
                43.800,
                7.500,
                7.500,
                {"amenity": "parking"},
                False,
            ),
        )
        con.commit()
        con.close()
        install_coverage_metadata(
            db,
            build_geofabrik_coverage_payload(_index(), ["monaco"]),
        )
        return td, db

    def test_coverage_stages_are_profiled_without_double_counting(self):
        td, db = self._database()
        con = connect(db)
        try:
            profile = QueryProfile()
            started = time.perf_counter()
            elements = execute_overpass_subset(
                con,
                "[out:json];nwr(around:100000,43.74,7.42)[amenity=parking];out center;",
                profile=profile,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
        finally:
            con.close()
            td.cleanup()
        self.assertEqual([element["id"] for element in elements], [101])
        self.assertGreater(profile.coverage_prepare_ms, 0.0)
        self.assertGreater(profile.coverage_filter_ms, 0.0)
        self.assertLessEqual(profile.timed_ms(), elapsed_ms + 1.0)

    def test_large_search_from_inside_never_exposes_unloaded_root(self):
        td, db = self._database()
        con = connect(db)
        try:
            elements = execute_overpass_subset(
                con,
                "[out:json];nwr(around:100000,43.74,7.42)[amenity=parking];out center;",
            )
        finally:
            con.close()
            td.cleanup()
        self.assertEqual([element["id"] for element in elements], [101])

    def test_auxiliary_search_anchor_outside_can_expand_back_into_loaded_coverage(self):
        td, db = self._database()
        con = connect(db)
        try:
            elements = execute_overpass_subset(
                con,
                "[out:json];nwr(around:20000,43.80,7.50)[amenity=parking];out center;",
            )
        finally:
            con.close()
            td.cleanup()
        # The outside-France candidate is filtered, while the Monaco candidate is
        # still discoverable once R&E expands the search radius far enough.
        self.assertEqual([element["id"] for element in elements], [101])

    def test_bbox_outside_loaded_coverage_returns_empty_not_error(self):
        td, db = self._database()
        con = connect(db)
        try:
            elements = execute_overpass_subset(
                con,
                "[out:json];nwr(43.79,7.48,43.81,7.52)[amenity=parking];out center;",
            )
        finally:
            con.close()
            td.cleanup()
        self.assertEqual(elements, [])


class CoverageGuardHttpTests(unittest.TestCase):
    def _server(self):
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "coverage.sqlite3"
        con = connect(db)
        seed_demo(con)
        con.close()
        install_coverage_metadata(
            db,
            build_geofabrik_coverage_payload(_index(), ["monaco"]),
        )
        state = State(db, db_readers=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return td, state, httpd, thread

    @staticmethod
    def _stop(td, state, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        state.close()
        thread.join(timeout=2)
        td.cleanup()

    @staticmethod
    def _request(port: int, query: str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/interpreter",
            data=query.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    @staticmethod
    def _events(text: str) -> list[dict]:
        parts = text.split(API_HEADER)
        return [json.loads(part.strip()) for part in parts[1:] if part.strip()]

    def test_strict_is_in_coverage_miss_still_returns_503_before_executor(self):
        td, state, httpd, thread = self._server()
        output = io.StringIO()
        query = (
            "[out:json];is_in(43.80,7.50)->.a;"
            "rel(pivot.a)[boundary=administrative][admin_level=2];out tags;"
        )
        try:
            with contextlib.redirect_stdout(output):
                with patch("local_osm.server.execute_overpass_subset") as execute:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        self._request(httpd.server_address[1], query)
                    self.assertEqual(caught.exception.code, 503)
                    payload = json.loads(caught.exception.read().decode("utf-8"))
                    self.assertEqual(payload["error"], "coverage_miss")
                    execute.assert_not_called()
                time.sleep(0.05)
        finally:
            self._stop(td, state, httpd, thread)

        event = self._events(output.getvalue())[0]
        self.assertEqual(event["status"], 503)
        self.assertEqual(event["coverage"]["classification"], "outside_loaded_coverage")
        self.assertEqual(event["error"]["type"], "coverage_miss")

    def test_around_outside_loaded_coverage_stays_200_and_executor_runs(self):
        td, state, httpd, thread = self._server()
        output = io.StringIO()
        query = "[out:json];nwr(around:2000,43.80,7.50)[building=detached];out center;"
        try:
            with contextlib.redirect_stdout(output):
                with patch("local_osm.server.execute_overpass_subset", return_value=[]) as execute:
                    with self._request(httpd.server_address[1], query) as response:
                        self.assertEqual(response.status, 200)
                        payload = json.load(response)
                        self.assertEqual(payload["elements"], [])
                        self.assertEqual(
                            payload["_local"]["coverage"]["classification"],
                            "around_anchor_outside_loaded_coverage",
                        )
                    execute.assert_called_once()
                time.sleep(0.05)
        finally:
            self._stop(td, state, httpd, thread)

        event = self._events(output.getvalue())[0]
        self.assertEqual(event["status"], 200)
        self.assertEqual(
            event["coverage"]["classification"],
            "around_anchor_outside_loaded_coverage",
        )
        self.assertNotIn("error", event)

    def test_mixed_bbox_stays_200_and_api_event_contains_debug_info(self):
        td, state, httpd, thread = self._server()
        output = io.StringIO()
        query = "[out:json];nwr(43.73,7.44,43.80,7.50)[building=yes];out center;"
        try:
            with contextlib.redirect_stdout(output):
                with patch("local_osm.server.execute_overpass_subset", return_value=[]):
                    with self._request(httpd.server_address[1], query) as response:
                        self.assertEqual(response.status, 200)
                        payload = json.load(response)
                        self.assertEqual(
                            payload["_local"]["coverage"]["classification"],
                            "bbox_loaded_unloaded_overlap",
                        )
                time.sleep(0.05)
        finally:
            self._stop(td, state, httpd, thread)

        event = self._events(output.getvalue())[0]
        self.assertEqual(event["status"], 200)
        self.assertEqual(
            event["coverage"]["classification"],
            "bbox_loaded_unloaded_overlap",
        )
        self.assertEqual(event["coverage"]["action"], "allowed_with_root_filter")


if __name__ == "__main__":
    unittest.main()

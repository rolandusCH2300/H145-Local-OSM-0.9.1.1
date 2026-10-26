from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from local_osm.db import connect, seed_demo
from local_osm.server import State, make_handler


class ServerTelemetryTests(unittest.TestCase):
    def _request(self, port: int, query: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/interpreter",
            data=query.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_response_exposes_lock_wait_and_execute(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                payload = self._request(
                    httpd.server_address[1],
                    "[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;",
                )
                local = payload["_local"]
                self.assertIn("query_ms", local)
                self.assertIn("lock_wait_ms", local)
                self.assertIn("execute_ms", local)
                self.assertGreaterEqual(local["lock_wait_ms"], 0.0)
                self.assertGreater(local["execute_ms"], 0.0)
                self.assertLessEqual(local["lock_wait_ms"] + local["execute_ms"], local["query_ms"] + 1.0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                thread.join(timeout=2)

    def test_lock_wait_is_not_folded_into_profile_unprofiled(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            first_inside = threading.Event()
            call_count = 0
            call_count_lock = threading.Lock()

            def slow_execute(con, query, profile=None):
                nonlocal call_count
                with call_count_lock:
                    call_count += 1
                    n = call_count
                if n == 1:
                    first_inside.set()
                    time.sleep(0.18)
                return []

            results: dict[str, dict] = {}

            def call(name: str):
                results[name] = self._request(
                    httpd.server_address[1],
                    "[out:json];node(around:1,60.4518,22.2666)[natural=cliff];out center;",
                )

            try:
                with patch("local_osm.server.execute_overpass_subset", side_effect=slow_execute):
                    t1 = threading.Thread(target=call, args=("first",))
                    t1.start()
                    self.assertTrue(first_inside.wait(timeout=2))
                    t2 = threading.Thread(target=call, args=("second",))
                    t2.start()
                    t1.join(timeout=3)
                    t2.join(timeout=3)

                second = results["second"]["_local"]
                self.assertGreater(second["lock_wait_ms"], 100.0)
                self.assertLess(second["execute_ms"], 50.0)
                self.assertLess(second["profile"]["unprofiled_ms"], 50.0)
                self.assertGreater(second["query_ms"], second["execute_ms"] + 100.0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                server_thread.join(timeout=2)

    def test_two_readers_avoid_head_of_line_wait(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db, db_readers=2)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            first_inside = threading.Event()
            call_count = 0
            call_count_lock = threading.Lock()

            def slow_first_execute(con, query, profile=None):
                nonlocal call_count
                with call_count_lock:
                    call_count += 1
                    n = call_count
                if n == 1:
                    first_inside.set()
                    time.sleep(0.18)
                return []

            results: dict[str, dict] = {}

            def call(name: str):
                results[name] = self._request(
                    httpd.server_address[1],
                    "[out:json];node(around:1,60.4518,22.2666)[natural=cliff];out center;",
                )

            try:
                with patch("local_osm.server.execute_overpass_subset", side_effect=slow_first_execute):
                    t1 = threading.Thread(target=call, args=("first",))
                    t1.start()
                    self.assertTrue(first_inside.wait(timeout=2))
                    t2 = threading.Thread(target=call, args=("second",))
                    t2.start()
                    t2.join(timeout=2)
                    t1.join(timeout=3)

                second = results["second"]["_local"]
                self.assertEqual(second["db_readers"], 2)
                self.assertIn("reader_wait_ms", second)
                self.assertLess(second["reader_wait_ms"], 80.0)
                self.assertLess(second["query_ms"], 100.0)
                self.assertLess(second["execute_ms"], 50.0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                server_thread.join(timeout=2)


    def test_activity_counter_detects_overlapping_http_requests(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db, db_readers=1)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            first_inside = threading.Event()
            call_count = 0
            call_count_lock = threading.Lock()

            def slow_execute(con, query, profile=None):
                nonlocal call_count
                with call_count_lock:
                    call_count += 1
                    n = call_count
                if n == 1:
                    first_inside.set()
                    time.sleep(0.18)
                return []

            results: dict[str, dict] = {}

            def call(name: str):
                results[name] = self._request(
                    httpd.server_address[1],
                    "[out:json];node(around:1,60.4518,22.2666)[natural=cliff];out center;",
                )

            try:
                with patch("local_osm.server.execute_overpass_subset", side_effect=slow_execute):
                    t1 = threading.Thread(target=call, args=("first",))
                    t1.start()
                    self.assertTrue(first_inside.wait(timeout=2))
                    t2 = threading.Thread(target=call, args=("second",))
                    t2.start()
                    time.sleep(0.05)
                    activity_while_blocked = state.query_activity()
                    t1.join(timeout=3)
                    t2.join(timeout=3)

                self.assertEqual(activity_while_blocked["active_query_requests"], 2)
                self.assertEqual(activity_while_blocked["max_active_query_requests_seen"], 2)
                self.assertEqual(activity_while_blocked["total_query_requests"], 2)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_address[1]}/health", timeout=5
                ) as response:
                    payload = json.load(response)

                self.assertEqual(payload["active_query_requests"], 0)
                self.assertEqual(payload["max_active_query_requests_seen"], 2)
                self.assertEqual(payload["total_query_requests"], 2)
                self.assertNotEqual(
                    results["first"]["_local"]["request_id"],
                    results["second"]["_local"]["request_id"],
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                server_thread.join(timeout=2)

    def test_sequential_requests_keep_max_active_at_one(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db, db_readers=1)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                query = "[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;"
                self._request(httpd.server_address[1], query)
                self._request(httpd.server_address[1], query)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_address[1]}/health", timeout=5
                ) as response:
                    payload = json.load(response)

                self.assertEqual(payload["active_query_requests"], 0)
                self.assertEqual(payload["max_active_query_requests_seen"], 1)
                self.assertEqual(payload["total_query_requests"], 2)
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                thread.join(timeout=2)

    def test_health_reports_reader_pool_size(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db, db_readers=4)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_address[1]}/health", timeout=5
                ) as response:
                    payload = json.load(response)
                self.assertEqual(payload["db_readers"], 4)
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

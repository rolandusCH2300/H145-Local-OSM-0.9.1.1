from __future__ import annotations

import contextlib
import io
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


API_HEADER = "[H145 Local OSM] API\n"


class ServerConsoleLoggingTests(unittest.TestCase):
    def _request(self, port: int, query: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/interpreter",
            data=query.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def _health(self, port: int) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            return json.load(response)

    def _server(self):
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "demo.sqlite3"
        con = connect(db)
        seed_demo(con)
        con.close()
        state = State(db, db_readers=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return td, state, httpd, thread

    def _stop_server(self, td, state, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        state.close()
        thread.join(timeout=2)
        td.cleanup()

    def _events(self, text: str) -> list[dict]:
        parts = text.split(API_HEADER)
        return [json.loads(part.strip()) for part in parts[1:] if part.strip()]

    def test_api_request_prints_one_pretty_json_event_without_legacy_noise(self):
        td, state, httpd, thread = self._server()
        query = "[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;"
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                self._request(httpd.server_address[1], query)
                time.sleep(0.05)
        finally:
            self._stop_server(td, state, httpd, thread)

        text = output.getvalue()
        self.assertEqual(text.count(API_HEADER), 1)
        self.assertNotIn("REQUEST START", text)
        self.assertNotIn("REQUEST END", text)
        self.assertNotIn("Query OK:", text)
        self.assertNotIn('"POST /api/interpreter HTTP/1.1"', text)
        self.assertNotIn("query_sha256", text)

        event = self._events(text)[0]
        self.assertEqual(event["method"], "POST")
        self.assertEqual(event["status"], 200)
        self.assertGreaterEqual(event["results"], 0)
        self.assertGreater(event["duration_ms"], 0.0)
        self.assertEqual(event["query"], query)
        self.assertIsInstance(event.get("timing_ms", {}), dict)
        self.assertIsInstance(event.get("path", []), list)
        self.assertNotIn("concurrency", event)

    def test_successful_health_request_is_silent(self):
        td, state, httpd, thread = self._server()
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                payload = self._health(httpd.server_address[1])
                time.sleep(0.05)
        finally:
            self._stop_server(td, state, httpd, thread)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(output.getvalue(), "")

    def test_overlapping_requests_add_concurrency_only_when_relevant(self):
        td, state, httpd, thread = self._server()
        first_inside = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        output = io.StringIO()

        def slow_execute(con, query, profile=None):
            nonlocal calls
            with calls_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_inside.set()
                time.sleep(0.18)
            return []

        query = "[out:json];node(around:1,60.4518,22.2666)[natural=cliff];out center;"

        def call():
            self._request(httpd.server_address[1], query)

        try:
            with contextlib.redirect_stdout(output):
                with patch("local_osm.server.execute_overpass_subset", side_effect=slow_execute):
                    t1 = threading.Thread(target=call)
                    t1.start()
                    self.assertTrue(first_inside.wait(timeout=2))
                    t2 = threading.Thread(target=call)
                    t2.start()
                    t1.join(timeout=3)
                    t2.join(timeout=3)
                time.sleep(0.05)
        finally:
            self._stop_server(td, state, httpd, thread)

        events = self._events(output.getvalue())
        self.assertEqual(len(events), 2)
        concurrent = [event for event in events if "concurrency" in event]
        self.assertTrue(concurrent)
        self.assertTrue(any(event["concurrency"]["active"] >= 2 for event in concurrent))
        self.assertTrue(any(event["concurrency"]["reader_wait_ms"] > 50 for event in concurrent))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from local_osm.db import connect, seed_demo
from local_osm.server import State, make_handler


class OverpassWireOrderRegressionTests(unittest.TestCase):
    def _request(self, port: int, query: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/interpreter",
            data=query.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_http_elements_use_overpass_asc_type_and_id_order(self):
        """R&E 0.997 must receive traditional Overpass node/way/relation order."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "demo.sqlite3"
            con = connect(db)
            seed_demo(con)
            con.close()

            state = State(db)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            # Deliberately mimic the local executor's pre-fix recursive order:
            # selected ways first, descendant nodes later, with unsorted IDs.
            unsorted = [
                {"type": "way", "id": 20, "nodes": [5, 2], "center": {"lat": 60.1, "lon": 24.9}, "tags": {"building": "house"}},
                {"type": "relation", "id": 30, "members": [], "center": {"lat": 60.2, "lon": 25.0}, "tags": {}},
                {"type": "node", "id": 5, "lat": 60.1, "lon": 24.9, "tags": {}},
                {"type": "way", "id": 10, "nodes": [2], "center": {"lat": 60.0, "lon": 24.8}, "tags": {"building": "apartments"}},
                {"type": "node", "id": 2, "lat": 60.0, "lon": 24.8, "tags": {}},
            ]

            try:
                with patch("local_osm.server.execute_overpass_subset", return_value=unsorted):
                    payload = self._request(
                        httpd.server_address[1],
                        "[out:json];nwr(around:1000,60.1,24.9)[building~'^(apartments|house)$'];(._;>;);out center;",
                    )

                self.assertEqual(
                    [(element["type"], element["id"]) for element in payload["elements"]],
                    [
                        ("node", 2),
                        ("node", 5),
                        ("way", 10),
                        ("way", 20),
                        ("relation", 30),
                    ],
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
                state.close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

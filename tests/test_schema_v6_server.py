from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_osm.db import OSMObject, connect
from local_osm.importer import ImportObjectBatch, _create_bulk_load_indexes, prepare_tier_c_schema
from local_osm.server import State, make_handler
from local_osm.tier_c import finalize_tier_c_tags


class SchemaV6ServerTests(unittest.TestCase):
    def test_schema_v6_health_and_query_work_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "schema-v6.sqlite3"
            con = connect(db)
            try:
                con.execute("PRAGMA foreign_keys=OFF")
                prepare_tier_c_schema(con)
                batch = ImportObjectBatch(con, object_limit=10, tag_limit=100)
                batch.add(OSMObject(
                    "node",
                    12345,
                    60.0,
                    24.0,
                    60.0,
                    60.0,
                    24.0,
                    24.0,
                    {"amenity": "hospital", "name": "Schema v6 Hospital"},
                    False,
                ))
                batch.flush()
                con.commit()
                _create_bulk_load_indexes(con)
                finalize_tier_c_tags(con)
                con.executemany(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                    (
                        ("schema_version", "6"),
                        ("profile", "h145-missions-v1"),
                    ),
                )
                con.commit()
            finally:
                con.close()

            state = State(db, db_readers=1)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/health", timeout=5) as response:
                    health = json.load(response)
                self.assertEqual(health["status"], "ready")
                self.assertEqual(health["service"], "H145 Local OSM")
                self.assertEqual(health["schema_version"], 6)
                self.assertEqual(health["objects"], 1)
                self.assertTrue(health["packed_tags_ready"])

                query = (
                    "[out:json];nwr(around:5000,60.0,24.0)"
                    "[amenity=hospital];out center;"
                )
                url = base + "/api/interpreter?data=" + urllib.parse.quote(query, safe="")
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.load(response)
                self.assertEqual(len(payload["elements"]), 1)
                element = payload["elements"][0]
                self.assertEqual(element["type"], "node")
                self.assertEqual(element["id"], 12345)
                self.assertEqual(element["tags"]["amenity"], "hospital")
                self.assertEqual(element["tags"]["name"], "Schema v6 Hospital")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                state.close()


if __name__ == "__main__":
    unittest.main()

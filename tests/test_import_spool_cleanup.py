import io
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_osm import importer
from local_osm import _importer_v6_base as core
from local_osm import tier_c


class ImportSpoolCleanupTests(unittest.TestCase):
    def _assert_failed_import_releases_resources(self, stage, *, finalize=False):
        with tempfile.TemporaryDirectory() as scratch:
            pbf = Path(scratch) / "input.osm.pbf"
            pbf.write_bytes(b"not read: the import stage is replaced by the test")
            database = Path(scratch) / "output.sqlite3"
            captured = {}

            def fail_stage(*args):
                con = args[0] if stage == "_drop_bulk_load_indexes" else args[2]
                captured["con"] = con
                captured["spool"] = tier_c._PACKED_SPOOLS[id(con)]
                if finalize:
                    tier_c.finalize_tier_c_tags(con)
                else:
                    tier_c.queue_packed_tag_rows(con, [(1, b'{"name":"test"}')])
                raise RuntimeError("controlled import failure")

            try:
                with (
                    patch.object(core, "_require_osmium", return_value=object()),
                    patch.object(core, stage, side_effect=fail_stage),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(RuntimeError, "controlled import failure"),
                ):
                    importer.import_pbf(pbf, database, workers=0, location_cache="flex_mem")

                self.assertNotIn(id(captured["con"]), tier_c._PACKED_SPOOLS)
                self.assertTrue(captured["spool"].file.closed)
                with self.assertRaises(sqlite3.ProgrammingError):
                    captured["con"].execute("SELECT 1")
            finally:
                # Keep a failing regression test from retaining a temporary file.
                if "con" in captured:
                    tier_c.discard_packed_tag_spool(captured["con"])
                    captured["con"].close()

    def test_discovery_failure_closes_and_discards_unfinished_spool(self):
        self._assert_failed_import_releases_resources("_discover")

    def test_setup_failure_after_schema_creation_releases_resources(self):
        self._assert_failed_import_releases_resources("_drop_bulk_load_indexes")

    def test_failure_after_finalization_preserves_original_error(self):
        self._assert_failed_import_releases_resources("_discover", finalize=True)


if __name__ == "__main__":
    unittest.main()

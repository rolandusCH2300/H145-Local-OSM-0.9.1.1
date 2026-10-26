import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_osm.db import connect, open_existing_query_db
from local_osm.importer import (
    SQLITE_BUILD_CACHE_KIB,
    SQLITE_BUILD_CACHE_MIB,
    _configure_build_sqlite_cache,
)


class ImportSQLiteCacheTests(unittest.TestCase):
    def test_build_cache_is_exactly_512_mib(self) -> None:
        con = sqlite3.connect(":memory:")
        try:
            effective_mib = _configure_build_sqlite_cache(con)
            effective_kib = int(con.execute("PRAGMA cache_size").fetchone()[0])
        finally:
            con.close()

        self.assertEqual(SQLITE_BUILD_CACHE_MIB, 512)
        self.assertEqual(SQLITE_BUILD_CACHE_KIB, 512 * 1024)
        self.assertEqual(effective_mib, 512)
        self.assertEqual(effective_kib, -(512 * 1024))

    def test_build_cache_does_not_persist_to_query_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache-scope.sqlite3"
            build_con = connect(path)
            try:
                default_kib = int(build_con.execute("PRAGMA cache_size").fetchone()[0])
                _configure_build_sqlite_cache(build_con)
                self.assertEqual(
                    int(build_con.execute("PRAGMA cache_size").fetchone()[0]),
                    -SQLITE_BUILD_CACHE_KIB,
                )
            finally:
                build_con.close()

            query_con = open_existing_query_db(path)
            try:
                self.assertEqual(
                    int(query_con.execute("PRAGMA cache_size").fetchone()[0]),
                    default_kib,
                )
            finally:
                query_con.close()


if __name__ == "__main__":
    unittest.main()

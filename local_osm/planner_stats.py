from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from .db import connect
from .import_profile import ANY_VALUE_KEYS, EXACT_VALUES

PLANNER_STATS_VERSION = "1"

DDL = r"""
CREATE TABLE IF NOT EXISTS planner_key_stats (
    key          TEXT PRIMARY KEY,
    object_count INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS planner_value_stats (
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    object_count INTEGER NOT NULL,
    PRIMARY KEY (key, value)
) WITHOUT ROWID;
"""


def planner_stats_ready(con: sqlite3.Connection) -> bool:
    try:
        row = con.execute(
            "SELECT value FROM metadata WHERE key='planner_stats_version'"
        ).fetchone()
        return row is not None and str(row[0]) == PLANNER_STATS_VERSION
    except sqlite3.OperationalError:
        return False


def build_planner_stats(
    con: sqlite3.Connection, *, verbose: bool = True
) -> tuple[int, int, float]:
    """Build exact cardinalities used by the adaptive query planner."""
    started = time.perf_counter()
    if verbose:
        print("[planner-stats] building R&E tag cardinalities...")

    # DDL is idempotent. Commit it before starting the rebuild transaction so
    # callers can safely chain this builder with other derived-index builders
    # that manage their own BEGIN IMMEDIATE transactions.
    con.executescript(DDL)
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM planner_key_stats")
        con.execute("DELETE FROM planner_value_stats")

        key_rows: list[tuple[str, int]] = []
        value_rows: list[tuple[str, str, int]] = []

        # Preserve value distributions for configured existence-query keys.
        for key in sorted(ANY_VALUE_KEYS):
            observed = [
                (str(row[0]), int(row[1]))
                for row in con.execute(
                    "SELECT value,COUNT(*) FROM object_tags INDEXED BY idx_tags_key_value "
                    "WHERE key=? GROUP BY value",
                    (key,),
                )
            ]
            total = sum(count for _value, count in observed)
            key_rows.append((key, total))
            value_rows.extend((key, value, count) for value, count in observed)

        # Store counts for every configured exact-value predicate.
        for key in sorted(EXACT_VALUES):
            for value in sorted(EXACT_VALUES[key]):
                row = con.execute(
                    "SELECT COUNT(*) FROM object_tags INDEXED BY idx_tags_key_value "
                    "WHERE key=? AND value=?",
                    (key, value),
                ).fetchone()
                value_rows.append((key, value, int(row[0]) if row is not None else 0))

        con.executemany(
            "INSERT INTO planner_key_stats(key,object_count) VALUES(?,?)",
            key_rows,
        )
        # A value may be both in an ANY_VALUE vocabulary and EXACT_VALUES in a
        # future profile. OR REPLACE keeps the builder robust to that overlap.
        con.executemany(
            "INSERT OR REPLACE INTO planner_value_stats(key,value,object_count) VALUES(?,?,?)",
            value_rows,
        )
        value_count = int(con.execute("SELECT COUNT(*) FROM planner_value_stats").fetchone()[0])
        key_count = int(con.execute("SELECT COUNT(*) FROM planner_key_stats").fetchone()[0])
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('planner_stats_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PLANNER_STATS_VERSION,),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('planner_stats_keys',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key_count),),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('planner_stats_values',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(value_count),),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise

    elapsed = time.perf_counter() - started
    if verbose:
        print(
            f"[planner-stats] complete: keys={key_count:,} values={value_count:,} "
            f"elapsed={elapsed:,.1f}s"
        )
    return key_count, value_count, elapsed


def get_persisted_tag_count(
    con: sqlite3.Connection, key: str, value: str | None
) -> int | None:
    """Return an exact persisted count, or None when this key isn't covered."""
    if not planner_stats_ready(con):
        return None
    try:
        if value is None:
            if key not in ANY_VALUE_KEYS:
                return None
            row = con.execute(
                "SELECT object_count FROM planner_key_stats WHERE key=?", (key,)
            ).fetchone()
            return None if row is None else int(row[0])
        if key not in ANY_VALUE_KEYS and key not in EXACT_VALUES:
            return None
        row = con.execute(
            "SELECT object_count FROM planner_value_stats WHERE key=? AND value=?",
            (key, value),
        ).fetchone()
        # A ready profile explicitly covers every queried exact value. Unknown
        # ANY_VALUE values are legitimately absent and therefore count as zero.
        return 0 if row is None else int(row[0])
    except sqlite3.OperationalError:
        return None


def get_persisted_values_count(
    con: sqlite3.Connection, key: str, values: tuple[str, ...]
) -> int | None:
    if not planner_stats_ready(con):
        return None
    if key not in ANY_VALUE_KEYS and key not in EXACT_VALUES:
        return None
    if not values:
        return 0
    try:
        total = 0
        # Small value lists use individual primary-key lookups.
        for value in values:
            row = con.execute(
                "SELECT object_count FROM planner_value_stats WHERE key=? AND value=?",
                (key, value),
            ).fetchone()
            if row is not None:
                total += int(row[0])
        return total
    except sqlite3.OperationalError:
        return None


def inspect_planner_stats(con: sqlite3.Connection) -> dict[str, int | bool]:
    ready = planner_stats_ready(con)
    if not ready:
        return {"ready": False, "keys": 0, "values": 0}
    try:
        keys = int(con.execute("SELECT COUNT(*) FROM planner_key_stats").fetchone()[0])
        values = int(con.execute("SELECT COUNT(*) FROM planner_value_stats").fetchone()[0])
    except sqlite3.OperationalError:
        return {"ready": False, "keys": 0, "values": 0}
    return {"ready": True, "keys": keys, "values": values}


def main() -> None:
    p = argparse.ArgumentParser(description="Build/check persistent R&E planner statistics")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    con = connect(args.db)
    try:
        if args.check:
            info = inspect_planner_stats(con)
            print(
                "Planner stats: "
                + (f"ready; keys={info['keys']:,}; values={info['values']:,}" if info["ready"] else "not built")
            )
            return
        build_planner_stats(con)
        con.commit()
        check = con.execute("PRAGMA quick_check").fetchone()
        print(f"[verify] SQLite quick_check: {check[0] if check else 'no result'}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

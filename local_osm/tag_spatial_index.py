from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

TAG_SPATIAL_INDEX_VERSION = 3


@dataclass(frozen=True, slots=True)
class TagSpatialFeature:
    code: int
    name: str
    key: str
    value: str
    table: str


# Derived tag-spatial indexes for exact-value predicates that benefit from
# spatial-first execution. They can be rebuilt without changing OSM storage.
FEATURES: tuple[TagSpatialFeature, ...] = (
    TagSpatialFeature(1, "building_yes", "building", "yes", "rne_ts_building_yes"),
    TagSpatialFeature(2, "building_detached", "building", "detached", "rne_ts_building_detached"),
    TagSpatialFeature(3, "landuse_farmland", "landuse", "farmland", "rne_ts_landuse_farmland"),
    TagSpatialFeature(4, "natural_water", "natural", "water", "rne_ts_natural_water"),
    TagSpatialFeature(5, "natural_cliff", "natural", "cliff", "rne_ts_natural_cliff"),
    TagSpatialFeature(6, "highway_path", "highway", "path", "rne_ts_highway_path"),
    TagSpatialFeature(7, "highway_track", "highway", "track", "rne_ts_highway_track"),
    TagSpatialFeature(8, "water_lake", "water", "lake", "rne_ts_water_lake"),
    TagSpatialFeature(9, "landuse_forest", "landuse", "forest", "rne_ts_landuse_forest"),
    TagSpatialFeature(10, "highway_cycleway", "highway", "cycleway", "rne_ts_highway_cycleway"),
    TagSpatialFeature(11, "natural_peak", "natural", "peak", "rne_ts_natural_peak"),
    TagSpatialFeature(12, "crossing_uncontrolled", "crossing", "uncontrolled", "rne_ts_crossing_uncontrolled"),
    TagSpatialFeature(13, "highway_crossing", "highway", "crossing", "rne_ts_highway_crossing"),
    TagSpatialFeature(14, "building_apartments", "building", "apartments", "rne_ts_building_apartments"),
    TagSpatialFeature(15, "amenity_parking", "amenity", "parking", "rne_ts_amenity_parking"),
)

FEATURE_BY_FILTER = {(f.key, f.value): f for f in FEATURES}


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def feature_for_filter(key: str, value: str) -> TagSpatialFeature | None:
    return FEATURE_BY_FILTER.get((str(key), str(value)))


def _create_tables(con: sqlite3.Connection) -> None:
    for feature in FEATURES:
        # Table names are defined by FEATURES.
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {feature.table} USING rtree("
            "object_pk, min_lon, max_lon, min_lat, max_lat)"
        )


def tag_spatial_index_ready(con: sqlite3.Connection) -> bool:
    try:
        row = con.execute(
            "SELECT value FROM metadata WHERE key='tag_spatial_index_version'"
        ).fetchone()
        if row is None or int(row[0]) != TAG_SPATIAL_INDEX_VERSION:
            return False
        row = con.execute(
            "SELECT value FROM metadata WHERE key='tag_spatial_index_features'"
        ).fetchone()
        if row is None or int(row[0]) != len(FEATURES):
            return False
        return all(_table_exists(con, feature.table) for feature in FEATURES)
    except (sqlite3.Error, TypeError, ValueError):
        return False


def build_tag_spatial_index(
    con: sqlite3.Connection, *, verbose: bool = True
) -> tuple[int, dict[str, int], float]:
    """Build/rebuild the optional H145 selective tag-spatial RTree bundle.

    Authoritative OSM rows/tags are never changed. The tables are derived
    acceleration data and query execution falls back to the generic planner if
    this bundle is absent or its metadata is incomplete.
    """
    started = time.perf_counter()
    if verbose:
        print(
            f"[tag-spatial] building {len(FEATURES)} selective H145 feature RTrees..."
        )

    _create_tables(con)
    counts: dict[str, int] = {}

    # Keep a rebuild atomic. CREATE VIRTUAL TABLE happens before the transaction
    # so a failure on a first build leaves harmless empty tables without ready
    # metadata; a failed rebuild of an existing bundle rolls back to old rows.
    con.execute("BEGIN IMMEDIATE")
    try:
        for feature in FEATURES:
            con.execute(f"DELETE FROM {feature.table}")
            con.execute(
                f"""
                INSERT INTO {feature.table}(object_pk,min_lon,max_lon,min_lat,max_lat)
                SELECT o.pk,o.min_lon,o.max_lon,o.min_lat,o.max_lat
                FROM object_tags t INDEXED BY idx_tags_key_value
                JOIN objects o ON o.pk=t.object_pk
                WHERE t.key=? AND t.value=?
                """,
                (feature.key, feature.value),
            )
            count = int(con.execute(f"SELECT COUNT(*) FROM {feature.table}").fetchone()[0])
            counts[feature.name] = count
            if verbose:
                print(
                    f"[tag-spatial] {feature.key}={feature.value}: {count:,} objects"
                )

        total = sum(counts.values())
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('tag_spatial_index_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(TAG_SPATIAL_INDEX_VERSION),),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('tag_spatial_index_features',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(len(FEATURES)),),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('tag_spatial_index_rows',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(total),),
        )
        for feature in FEATURES:
            con.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    f"tag_spatial_rows_{feature.name}",
                    str(counts[feature.name]),
                ),
            )
        con.commit()
    except BaseException:
        con.rollback()
        raise

    elapsed = time.perf_counter() - started
    if verbose:
        print(
            f"[tag-spatial] complete: rows={total:,} elapsed={elapsed:,.1f}s"
        )
    return total, counts, elapsed


def inspect_tag_spatial_index(con: sqlite3.Connection) -> dict[str, object]:
    ready = tag_spatial_index_ready(con)
    counts: dict[str, int] = {}
    total = 0
    for feature in FEATURES:
        if _table_exists(con, feature.table):
            try:
                count = int(con.execute(f"SELECT COUNT(*) FROM {feature.table}").fetchone()[0])
            except sqlite3.Error:
                count = 0
        else:
            count = 0
        counts[feature.name] = count
        total += count
    return {
        "ready": ready,
        "version": TAG_SPATIAL_INDEX_VERSION if ready else 0,
        "features": len(FEATURES) if ready else 0,
        "rows": total,
        "counts": counts,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Build/check the optional compact H145 selective tag-spatial RTrees "
            "without re-importing OSM."
        )
    )
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    db = args.db.resolve()
    if not db.is_file():
        raise SystemExit(f"Database not found: {db}")

    size_before = db.stat().st_size
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        if args.check:
            info = inspect_tag_spatial_index(con)
            print(
                "Tag-spatial index: "
                f"{'ready' if info['ready'] else 'not ready'}; "
                f"features={int(info['features']):,}; rows={int(info['rows']):,}; "
                f"version={int(info['version'])}"
            )
            if info["ready"]:
                counts = info["counts"]
                assert isinstance(counts, dict)
                for feature in FEATURES:
                    print(
                        f"  {feature.key}={feature.value}: "
                        f"{int(counts.get(feature.name, 0)):,}"
                    )
            return

        build_tag_spatial_index(con)
        check = con.execute("PRAGMA quick_check").fetchone()
        print(f"[verify] SQLite quick_check: {check[0] if check else 'no result'}")
        size_after = db.stat().st_size
        delta = size_after - size_before
        print(
            f"[tag-spatial] database size delta: {delta / (1024 * 1024):+,.2f} MiB "
            f"({size_before / (1024**3):,.2f} -> {size_after / (1024**3):,.2f} GiB)"
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()

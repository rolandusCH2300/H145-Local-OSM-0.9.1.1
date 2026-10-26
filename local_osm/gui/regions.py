from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from statistics import median

# Empirical source-PBF to Local OSM SQLite size ratios.
_DB_SIZE_RATIOS = (
    7_429_508 / (722_286 + 1_341_033),
    14.0 / 3.2,
    8_216_928 / (722_286 + 794_806),
)
DB_ESTIMATE_MULTIPLIER = median(_DB_SIZE_RATIOS)
DB_ESTIMATE_LOW_MULTIPLIER = min(_DB_SIZE_RATIOS)
DB_ESTIMATE_HIGH_MULTIPLIER = max(_DB_SIZE_RATIOS)


def conflicting_region_ids(
    records: Sequence[dict],
    selected_ids: Iterable[str],
) -> set[str]:
    """Return selected Geofabrik ids plus all ancestors and descendants.

    Geofabrik parent/child extracts overlap spatially. Treating the whole
    hierarchy as mutually exclusive prevents combinations such as Europe +
    Finland or Germany + Bavaria while still allowing unrelated siblings.
    """
    parent_by_id: dict[str, str | None] = {}
    children_by_id: dict[str, set[str]] = defaultdict(set)

    for record in records:
        region_id = str(record.get("id") or "")
        if not region_id:
            continue
        parent_raw = record.get("parent")
        parent_id = str(parent_raw) if parent_raw else None
        parent_by_id[region_id] = parent_id
        if parent_id:
            children_by_id[parent_id].add(region_id)

    conflicts: set[str] = set()
    for raw_selected in selected_ids:
        selected = str(raw_selected or "")
        if not selected:
            continue
        conflicts.add(selected)

        seen_ancestors: set[str] = set()
        parent = parent_by_id.get(selected)
        while parent and parent not in seen_ancestors:
            seen_ancestors.add(parent)
            conflicts.add(parent)
            parent = parent_by_id.get(parent)

        seen_descendants: set[str] = set()
        stack = list(children_by_id.get(selected, ()))
        while stack:
            child = stack.pop()
            if child in seen_descendants:
                continue
            seen_descendants.add(child)
            conflicts.add(child)
            stack.extend(children_by_id.get(child, ()))

    return conflicts


def direct_children(records: Sequence[dict], parent_id: str) -> list[dict]:
    """Return the direct downloadable children of one Geofabrik region."""
    wanted = str(parent_id or "")
    values = [
        record
        for record in records
        if str(record.get("parent") or "") == wanted
        and record.get("id")
        and record.get("pbf")
    ]
    return sorted(
        values,
        key=lambda record: str(record.get("name") or record.get("id") or "").casefold(),
    )


def selectable_parent_records(records: Sequence[dict]) -> list[dict]:
    """Return regions suitable for the left side of a parent -> child row.

    Top-level extracts are always included. Nested extracts are included when
    they have direct children, which lets users select e.g. Europe -> Germany
    or Germany -> Bavaria while keeping the row model to two dropdowns.
    """
    records_by_id = {
        str(record.get("id") or ""): record
        for record in records
        if record.get("id") and record.get("pbf")
    }
    parents_with_children = {
        str(record.get("parent"))
        for record in records
        if record.get("parent")
    }
    values = [
        record
        for region_id, record in records_by_id.items()
        if not record.get("parent") or region_id in parents_with_children
    ]
    return sorted(
        values,
        key=lambda record: str(record.get("name") or record.get("id") or "").casefold(),
    )


def region_selections_conflict(
    records: Sequence[dict],
    selected_ids: Sequence[str],
) -> bool:
    """Return True when any selected extracts overlap by ancestry."""
    selected = [str(value) for value in selected_ids if value]
    for index, region_id in enumerate(selected):
        others = selected[:index] + selected[index + 1 :]
        if region_id in conflicting_region_ids(records, others):
            return True
    return False


def estimate_database_size(source_bytes: int) -> tuple[int, int, int]:
    """Return (central, low, high) empirical Local OSM DB byte estimates."""
    size = max(0, int(source_bytes))
    return (
        round(size * DB_ESTIMATE_MULTIPLIER),
        round(size * DB_ESTIMATE_LOW_MULTIPLIER),
        round(size * DB_ESTIMATE_HIGH_MULTIPLIER),
    )

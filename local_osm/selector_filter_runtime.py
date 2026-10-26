from __future__ import annotations

"""Runtime bridge between Tier-C selector tags and authoritative full tags.

Schema v6 deliberately keeps only the frozen selector/index subset in
``object_tags`` and preserves every original OSM tag in ``packed_object_tags``.
The query planner may use only predicates whose truth can be decided from the
normalized selector subset. Secondary predicates such as ``[name]`` or
``[surface!=...]`` must be evaluated against the packed full-tag payload after
an indexed candidate set has been found.
"""

import re
import sqlite3
import time
from dataclasses import replace
from functools import lru_cache

from .hydration_cache import load_packed_tags_many, packed_tags_ready
from .import_profile import ANY_VALUE_KEYS, EXACT_VALUES, PROFILE_NAME


_SIMPLE_FINITE_REGEX_RE = re.compile(
    r"^\^?\(?([A-Za-z0-9_:.-]+(?:\|[A-Za-z0-9_:.-]+)+)\)?\$?$"
)
_INSTALLED = False


@lru_cache(maxsize=16)
def _selector_only_tags_ready(con: sqlite3.Connection) -> bool:
    """Return whether ``object_tags`` is the Tier-C selector-only projection."""
    try:
        metadata = {
            str(row[0]): str(row[1])
            for row in con.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('selector_tags_complete','selector_tags_profile')"
            )
        }
    except sqlite3.Error:
        return False
    return (
        metadata.get("selector_tags_complete") == "1"
        and metadata.get("selector_tags_profile") == PROFILE_NAME
    )


def _finite_regex_values(pattern: str | None) -> tuple[str, ...] | None:
    if not pattern:
        return None
    match = _SIMPLE_FINITE_REGEX_RE.fullmatch(pattern.strip())
    if not match:
        return None
    values = tuple(sorted(set(match.group(1).split("|"))))
    return values or None


def _filter_is_selector_complete(tag_filter) -> bool:
    """Whether object_tags alone can decide this predicate on schema v6."""
    key = str(tag_filter.key)
    if key in ANY_VALUE_KEYS:
        # Every value of these keys is normalized, so exists/equality/negative/
        # regex predicates all have complete information in object_tags.
        return True

    retained = EXACT_VALUES.get(key)
    if not retained:
        return False

    if tag_filter.op == "=":
        return (tag_filter.value or "") in retained

    if tag_filter.op == "~":
        # The mission audit recognizes this same finite category grammar. If
        # every possible value is retained, the normalized vocabulary is exact.
        values = _finite_regex_values(tag_filter.value)
        return values is not None and all(value in retained for value in values)

    # Existence and negative predicates need knowledge of values that the exact-
    # value profile intentionally omits, so they must use authoritative tags.
    return False


def install_selector_filter_runtime() -> None:
    """Make Tier-C candidate filtering honor full-tag Overpass semantics."""
    global _INSTALLED
    if _INSTALLED:
        return

    from . import query as query_mod

    original_candidate_rows = query_mod._candidate_rows

    def runtime_candidate_rows(
        con: sqlite3.Connection,
        sel,
        profile=None,
    ):
        if not _selector_only_tags_ready(con):
            return original_candidate_rows(con, sel, profile)

        indexed_filters = tuple(
            item for item in sel.filters if _filter_is_selector_complete(item)
        )
        full_tag_filters = tuple(
            item for item in sel.filters if not _filter_is_selector_complete(item)
        )
        if not full_tag_filters:
            return original_candidate_rows(con, sel, profile)

        # The mission coverage audit guarantees at least one retained anchor for
        # every installed selector branch. Keeping only those complete predicates
        # here prevents omitted metadata (for example [name]) from becoming a
        # false zero-row seed while preserving the existing adaptive planner.
        indexed_sel = replace(sel, filters=indexed_filters)
        rows = original_candidate_rows(con, indexed_sel, profile)
        if not rows:
            return rows

        if not packed_tags_ready(con):
            raise RuntimeError(
                "Tier-C selector-only object_tags requires complete packed full tags "
                "for secondary query predicates"
            )

        started = time.perf_counter()
        tags_by_pk, _sql_ms, _decode_ms = load_packed_tags_many(
            con, (int(row["pk"]) for row in rows)
        )
        kept = [
            row
            for row in rows
            if all(
                query_mod._filter_matches(tags_by_pk.get(int(row["pk"]), {}), item)
                for item in full_tag_filters
            )
        ]
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if profile is not None:
            profile.candidate_filter_ms += elapsed_ms
            profile.packed_tags_calls += 1
            # The wrapped candidate path already counted every indexed candidate
            # as matched. Correct that diagnostic to the authoritative result.
            profile.matched_rows -= len(rows) - len(kept)

        return kept

    query_mod._candidate_rows = runtime_candidate_rows
    query_mod._selector_filter_runtime_original_candidate_rows = original_candidate_rows
    query_mod._selector_filter_runtime_installed = True
    _INSTALLED = True

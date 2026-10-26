from __future__ import annotations

import codecs
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


RNE_PACKAGE = "hpg-airbus-h145-z-andrew-mission-pack"
RNE_RELATIVE_PATH = Path(
    RNE_PACKAGE,
    "hpgmission",
    "RANDOM AND EVERYWHERE",
    "everywhere_all.json",
)
RNE_BACKUP_SUFFIX = ".h145localosm.backup"
SUPPORTED_RNE_VERSION = "0.997"

_FORCED_CICERS = {
    "set": {"var": ["L:{local:HXX}_PERSIST_MISSION_ENDPOINT_OPTION", "number"]},
    "value": 3,
}
_COOLDOWN_CICERS = {"set": {"local": "cooldowntimer"}, "value": 10}
_CALL_RANDOMIZE = {"call_macro": "randomize"}
_CALL_UPDATE_TRACK = {"call_macro": "UpdateRescueTrack"}
_CALL_CICERS_PING = {"call_macro": "CICERS PING"}
_CALL_CICERS_LINKS = {"call_macro": "CICERS LINKS"}
_CALL_LOCATION_RESET = {"call_macro": "location reset"}
_CALL_ENSURE_QUERY = {"call_macro": "ensure data query service selection"}
_CLEAR_DISPATCHER_TEXT = {"set": {"local": "Dispatcher_text"}, "value": ""}
_DISPLAY_DISPATCH = {"set": {"local": "DISPLAY_DISPATCH"}, "value": 1}
_UNLOCK_DISPATCH = {"set": {"local": "lock_random_dispatch"}, "value": 0}
_CICERS_REFRESH_CONDITION = {
    "and": [
        {"require": {"local": "CICERS_REMAINING_S"}, "lt": 3600},
        {"require": {"global": "DATAQUERYSERVICE"}, "eq": 3},
    ]
}
_LOCATION_RESET_COMMANDS = [
    _CALL_LOCATION_RESET,
    {"set": {"local": "has_location"}, "value": "no"},
    {"set": {"local": "has_hospital_location"}, "value": "no"},
    {"set": {"local": "timer_location"}, "value": None},
    {"set": {"local": "queryfailed"}, "value": None},
    {"set": {"local": "query_skipped"}, "value": None},
]
_TITLE_OVERPASS = re.compile(r'"title"\s*:\s*"Overpass DE"')
_TITLE_LOCAL_OSM = re.compile(r'"title"\s*:\s*"Local OSM"')
_TITLE_NOT_DELAYED = re.compile(r'"title"\s*:\s*"NOT DELAYED"')

_JSON_DECODER = json.JSONDecoder()
_JSON_WHITESPACE = " \t\r\n"


@dataclass(frozen=True, slots=True)
class RNEPatchStatus:
    mission_file: Path | None
    version: str | None
    patched: bool
    backup_available: bool
    supported: bool
    detail: str = ""


def find_rne_mission_file(community: Path | None) -> Path | None:
    if community is None or not Path(community).is_dir():
        return None

    root = Path(community).resolve()
    exact = root / RNE_RELATIVE_PATH
    if exact.is_file():
        return exact.resolve()

    try:
        candidates = []
        for path in root.rglob("everywhere_all.json"):
            if not path.is_file():
                continue
            parts = [part.casefold() for part in path.parts]
            if RNE_PACKAGE.casefold() not in parts:
                continue
            if "random and everywhere" not in parts:
                continue
            candidates.append(path)
    except OSError:
        return None

    return candidates[0].resolve() if len(candidates) == 1 else None


def _rne_version(payload: dict) -> str | None:
    if payload.get("id") != "HEMSRandE":
        return None
    macros = payload.get("macros")
    if not isinstance(macros, dict):
        return None
    commands = macros.get("version check")
    if not isinstance(commands, list):
        return None
    for command in commands:
        if not isinstance(command, dict):
            continue
        target = command.get("set")
        if isinstance(target, dict) and target.get("local") == "MISSION_VERSION":
            value = command.get("value")
            return str(value) if value is not None else None
    return None


def _read_rne_text(path: Path) -> tuple[str, str, bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    text = raw.decode("utf-8-sig")

    if "\r\n" in text:
        newline = "\r\n"
    elif "\r" in text:
        newline = "\r"
    else:
        newline = "\n"

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, newline, has_bom


def _write_rne_text(
    path: Path,
    text: str,
    newline: str,
    has_bom: bool,
) -> None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    serialized = normalized if newline == "\n" else normalized.replace("\n", newline)
    raw = serialized.encode("utf-8")
    if has_bom:
        raw = codecs.BOM_UTF8 + raw
    path.write_bytes(raw)


def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        return ""
    return text[start:end]


def _has_legacy_patch_markers(text: str) -> bool:
    """Markers written by the original text patch on the stock 0.997 layout."""
    ping = _section(text, '"CICERS PING":[', '"DATAQUERYSERVICERANDOM":[')
    next_dispatch = _section(
        text,
        '"next_dispatch":[',
        '"HELOVICTIMS refinement":[',
    )
    forced_cicers = (
        '{"set": {"var": ["L:{local:HXX}_PERSIST_MISSION_ENDPOINT_OPTION", '
        '"number"]}, "value":3},'
    )

    return (
        forced_cicers not in ping
        and '"title":"Overpass DE"' not in text
        and text.count('"title":"Local OSM"') >= 2
        and "OSM Service: Overpass DE" not in text
        and "OSM Service: Local OSM" in text
        and '"title":"NOT DELAYED"' not in text
        and '{"if": {"global": "REALISTIC_DISPATCH"}, "ne":1, "then": [' in text
        and '{"if": {"global":"DATAQUERYSERVICE"}, "eq": 0' in next_dispatch
        and '{"call_macro": "location reset"}' in next_dispatch
        and '{"set": {"local":"lock_random_dispatch"}, "value":0}' in next_dispatch
        and '{"set": {"local":"DISPLAY_DISPATCH"}, "value":1}' in next_dispatch
    )


def _walk_objects(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_objects(item)


def _payload_is_patched(text: str, payload: dict) -> bool:
    """Layout-independent check: same markers as the text patch, read from the JSON."""
    macros = payload.get("macros")
    if not isinstance(macros, dict):
        return False
    ping = macros.get("CICERS PING")
    next_dispatch = macros.get("next_dispatch")
    if not isinstance(ping, list) or not isinstance(next_dispatch, list):
        return False

    dispatch_defaults = [
        node
        for node in _walk_objects(macros)
        if node.get("if") == {"global": "REALISTIC_DISPATCH"} and "then" in node
    ]
    next_objects = list(_walk_objects(next_dispatch))

    return (
        all(node != _FORCED_CICERS for node in _walk_objects(ping))
        and not _TITLE_OVERPASS.search(text)
        and len(_TITLE_LOCAL_OSM.findall(text)) >= 2
        and "OSM Service: Overpass DE" not in text
        and "OSM Service: Local OSM" in text
        and not _TITLE_NOT_DELAYED.search(text)
        and any(node.get("ne") == 1 for node in dispatch_defaults)
        and not any("eq" in node and node["eq"] is None for node in dispatch_defaults)
        and any(
            node.get("if") == {"global": "DATAQUERYSERVICE"} and node.get("eq") == 0
            for node in next_objects
        )
        and _CALL_LOCATION_RESET in next_objects
        and _UNLOCK_DISPATCH in next_objects
        and _DISPLAY_DISPATCH in next_objects
    )


def _is_patched(text: str, version: str | None) -> bool:
    if version != SUPPORTED_RNE_VERSION:
        return False
    if _has_legacy_patch_markers(text):
        return True
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and _payload_is_patched(text, payload)


def rne_patch_status(community: Path | None) -> RNEPatchStatus:
    path = find_rne_mission_file(community)
    if path is None:
        return RNEPatchStatus(
            None,
            None,
            False,
            False,
            False,
            "R&E everywhere_all.json not found",
        )

    backup = Path(str(path) + RNE_BACKUP_SUFFIX)
    try:
        text, _newline, _has_bom = _read_rne_text(path)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("R&E mission file root is not an object")
        version = _rne_version(payload)
        if version is None:
            raise ValueError("R&E mission identity/version could not be verified")
    except Exception as exc:
        return RNEPatchStatus(
            path,
            None,
            False,
            backup.is_file(),
            False,
            f"{type(exc).__name__}: {exc}",
        )

    return RNEPatchStatus(
        path,
        version,
        _is_patched(text, version),
        backup.is_file(),
        version == SUPPORTED_RNE_VERSION,
        "",
    )


# --- Layout-independent JSON editing -------------------------------------
#
# The mission file is edited in place: values are located by scanning the
# JSON text, so only the commands that change are rewritten and the rest of
# the file keeps its original formatting, whatever that formatting is.


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _JSON_WHITESPACE:
        index += 1
    return index


def _decode_at(text: str, start: int) -> tuple[object, int]:
    try:
        return _JSON_DECODER.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"R&E mission file is not valid JSON: {exc}") from None


def _value_end(text: str, start: int) -> int:
    return _decode_at(text, start)[1]


def _array_items(text: str, start: int) -> tuple[tuple[int, int], ...]:
    items = []
    index = _skip_ws(text, start + 1)
    if text[index] == "]":
        return ()
    while True:
        end = _value_end(text, index)
        items.append((index, end))
        index = _skip_ws(text, end)
        if text[index] == ",":
            index = _skip_ws(text, index + 1)
        elif text[index] == "]":
            return tuple(items)
        else:
            raise ValueError("R&E mission file has a malformed array")


def _object_members(
    text: str,
    start: int,
    stop_at: str | None = None,
) -> Iterator[tuple[str, int, int]]:
    """Yield ``(key, value_start, value_end)``.

    For the ``stop_at`` key, ``value_end`` is -1 and the scan stops there, so
    a large value such as the macros object is not decoded just to find it.
    """
    index = _skip_ws(text, start + 1)
    if text[index] == "}":
        return
    while True:
        key, index = _decode_at(text, index)
        index = _skip_ws(text, index)
        if text[index] != ":":
            raise ValueError("R&E mission file has a malformed object")
        value_start = _skip_ws(text, index + 1)
        if key == stop_at:
            yield key, value_start, -1
            return
        value_end = _value_end(text, value_start)
        yield key, value_start, value_end
        index = _skip_ws(text, value_end)
        if text[index] == ",":
            index = _skip_ws(text, index + 1)
        elif text[index] == "}":
            return
        else:
            raise ValueError("R&E mission file has a malformed object")


def _macros(text: str, stop_at: str | None = None) -> Iterator[tuple[str, int, int]]:
    for key, start, _end in _object_members(text, _skip_ws(text, 0), "macros"):
        if key == "macros" and text[start] == "{":
            yield from _object_members(text, start, stop_at)
            return
    raise ValueError("R&E macros were not found")


def _macro_start(text: str, name: str) -> int:
    for macro, start, _end in _macros(text, stop_at=name):
        if macro == name and text[start] == "[":
            return start
    raise ValueError(f"R&E macro {name!r} was not found")


@dataclass(frozen=True, slots=True)
class _Element:
    items: tuple[tuple[int, int], ...]
    index: int
    value: object

    @property
    def start(self) -> int:
        return self.items[self.index][0]

    @property
    def end(self) -> int:
        return self.items[self.index][1]


def _find_elements(
    text: str,
    start: int,
    needle: str,
    predicate: Callable[[dict], bool],
) -> list[_Element]:
    """Array elements (objects) below ``start`` matching ``predicate``.

    ``needle`` is a substring every match contains; it prunes the scan so
    only the relevant parts of the file are parsed.
    """
    found: list[_Element] = []
    if text[start] == "[":
        items = _array_items(text, start)
        for index, (item_start, item_end) in enumerate(items):
            if text.find(needle, item_start, item_end) < 0:
                continue
            if text[item_start] == "{":
                value = json.loads(text[item_start:item_end])
                if predicate(value):
                    found.append(_Element(items, index, value))
                    continue
            if text[item_start] in "[{":
                found.extend(_find_elements(text, item_start, needle, predicate))
    elif text[start] == "{":
        for _key, value_start, value_end in _object_members(text, start):
            if text[value_start] in "[{" and text.find(needle, value_start, value_end) >= 0:
                found.extend(_find_elements(text, value_start, needle, predicate))
    return found


def _find_in_macros(
    text: str,
    needle: str,
    predicate: Callable[[dict], bool],
) -> list[_Element]:
    found: list[_Element] = []
    for _name, start, end in _macros(text):
        if text[start] in "[{" and text.find(needle, start, end) >= 0:
            found.extend(_find_elements(text, start, needle, predicate))
    return found


def _one(elements: list[_Element], label: str) -> _Element:
    if len(elements) != 1:
        raise ValueError(f"R&E {label} pattern mismatch ({len(elements)} matches)")
    return elements[0]


def _item_value(text: str, element: _Element, index: int) -> object:
    start, end = element.items[index]
    return json.loads(text[start:end])


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _separator(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    return "\n" + indent if not indent.strip() else " "


def _remove_items(text: str, element: _Element, first: int, last: int) -> str:
    items = element.items
    start, end = items[first][0], items[last][1]
    if last + 1 < len(items):
        return text[:start] + text[items[last + 1][0]:]
    if first > 0:
        return text[: items[first - 1][1]] + text[end:]
    return text[:start] + text[end:]


def _remove(text: str, element: _Element) -> str:
    return _remove_items(text, element, element.index, element.index)


def _replace(text: str, element: _Element, value: object) -> str:
    return text[: element.start] + _dump(value) + text[element.end:]


def _insert_before(text: str, element: _Element, values: list[object]) -> str:
    separator = _separator(text, element.start)
    block = "".join(_dump(value) + "," + separator for value in values)
    return text[: element.start] + block + text[element.start:]


def _insert_after(text: str, element: _Element, values: list[object]) -> str:
    separator = _separator(text, element.start)
    block = "".join("," + separator + _dump(value) for value in values)
    return text[: element.end] + block + text[element.end:]


def _is_cicers_cooldown(value: dict) -> bool:
    # Stock files set a literal 10 s; reworked files read the delay from a
    # global such as NEXT_DISPATCH_COOLDOWN_FAST.
    then = value.get("then")
    return (
        value.get("if") == {"global": "DATAQUERYSERVICE"}
        and value.get("eq") == 3
        and isinstance(then, list)
        and len(then) == 1
        and isinstance(then[0], dict)
        and then[0].get("set") == _COOLDOWN_CICERS["set"]
    )


def _patch_rne_text(text: str) -> str:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("R&E mission file root is not an object")
    version = _rne_version(payload)
    if version != SUPPORTED_RNE_VERSION:
        raise ValueError(
            f"R&E {version or 'unknown'} is not supported by this compatibility patch"
        )
    if _is_patched(text, version):
        return text
    _macro_start(text, "location reset")

    # The CICERS ping no longer switches the endpoint to CICERS OSM.
    forced = _one(
        _find_elements(
            text,
            _macro_start(text, "CICERS PING"),
            "PERSIST_MISSION_ENDPOINT_OPTION",
            lambda value: value == _FORCED_CICERS,
        ),
        "CICERS endpoint",
    )
    text = _remove(text, forced)

    # No cooldown between dispatches when the Local OSM endpoint is selected.
    cooldown = _one(
        _find_elements(
            text,
            _macro_start(text, "next_dispatch"),
            "cooldowntimer",
            _is_cicers_cooldown,
        ),
        "Local OSM cooldown",
    )
    text = _replace(
        text,
        cooldown,
        {
            "if": {"global": "DATAQUERYSERVICE"},
            "eq": 0,
            "then": [{"set": {"local": "cooldowntimer"}, "value": 0}],
            "else": [cooldown.value],
        },
    )

    # Reset the previous locations before randomizing, and do not show the
    # dispatch before it is ready.
    randomize = _one(
        _find_elements(
            text,
            _macro_start(text, "next_dispatch"),
            '"randomize"',
            lambda value: value == _CALL_RANDOMIZE,
        ),
        "next dispatch reset",
    )
    early_display = [
        index
        for index in range(randomize.index + 1, len(randomize.items))
        if _item_value(text, randomize, index) == _DISPLAY_DISPATCH
    ]
    if len(early_display) != 1:
        raise ValueError(
            f"R&E early dispatch display pattern mismatch ({len(early_display)} matches)"
        )
    text = _remove_items(text, randomize, early_display[0], early_display[0])
    text = _insert_before(text, randomize, _LOCATION_RESET_COMMANDS)

    # Unlock and show the dispatch once it has been fully prepared.
    update = _one(
        [
            element
            for element in _find_elements(
                text,
                _macro_start(text, "next_dispatch"),
                "UpdateRescueTrack",
                lambda value: value == _CALL_UPDATE_TRACK,
            )
            if element.index == len(element.items) - 1
            and element.index > 0
            and _item_value(text, element, element.index - 1) == _CLEAR_DISPATCHER_TEXT
        ],
        "next dispatch unlock",
    )
    text = _insert_after(text, update, [_UNLOCK_DISPATCH, _DISPLAY_DISPATCH])

    labels = len(_TITLE_OVERPASS.findall(text))
    if labels != 2:
        raise ValueError(f"R&E endpoint label pattern mismatch ({labels} matches)")
    text = _TITLE_OVERPASS.sub('"title":"Local OSM"', text)
    status_labels = text.count("OSM Service: Overpass DE")
    if status_labels != 1:
        raise ValueError(
            f"R&E endpoint status label pattern mismatch ({status_labels} matches)"
        )
    text = text.replace("OSM Service: Overpass DE", "OSM Service: Local OSM")

    not_delayed = _one(
        _find_in_macros(
            text,
            "NOT DELAYED",
            lambda value: value.get("title") == "NOT DELAYED",
        ),
        "dispatch mode",
    )
    text = _remove(text, not_delayed)

    refresh = _one(
        _find_in_macros(
            text,
            "CICERS_REMAINING_S",
            lambda value: value.get("if") == _CICERS_REFRESH_CONDITION
            and value.get("then") == [_CALL_CICERS_PING],
        ),
        "automatic CICERS refresh",
    )
    text = _remove(text, refresh)

    dispatch_default = _one(
        _find_in_macros(
            text,
            "REALISTIC_DISPATCH",
            lambda value: value.get("if") == {"global": "REALISTIC_DISPATCH"}
            and "eq" in value
            and value["eq"] is None
            and "then" in value,
        ),
        "dispatch default",
    )
    text = _replace(
        text,
        dispatch_default,
        {
            ("ne" if key == "eq" else key): (1 if key == "eq" else value)
            for key, value in dispatch_default.value.items()
        },
    )

    # Automatic CICERS ping and links at mission start. The stock file calls
    # them back to back; reworked files ping from the data query selection
    # macro called right before the links.
    links = _one(
        _find_in_macros(
            text,
            "CICERS LINKS",
            lambda value: value == _CALL_CICERS_LINKS,
        ),
        "automatic CICERS startup",
    )
    previous = _item_value(text, links, links.index - 1) if links.index > 0 else None
    if previous == _CALL_CICERS_PING:
        text = _remove_items(text, links, links.index - 1, links.index)
    elif previous == _CALL_ENSURE_QUERY:
        ensure_start = _macro_start(text, "ensure data query service selection")
        ensure_items = _array_items(text, ensure_start)
        ping = _one(
            [
                element
                for element in _find_elements(
                    text,
                    ensure_start,
                    "CICERS PING",
                    lambda value: value == _CALL_CICERS_PING,
                )
                if element.items == ensure_items
            ],
            "automatic CICERS startup ping",
        )
        # Both calls live in different macros: removing the later one first
        # keeps the offsets of the other one valid.
        for element in sorted((links, ping), key=lambda item: item.start, reverse=True):
            text = _remove(text, element)
    else:
        raise ValueError("R&E automatic CICERS startup ping was not found")

    patched_payload = json.loads(text)
    patched_version = _rne_version(patched_payload)
    if not _is_patched(text, patched_version):
        raise ValueError("patched R&E verification failed")
    return text


def patch_rne_mission(community: Path) -> RNEPatchStatus:
    status = rne_patch_status(community)
    if status.mission_file is None:
        raise FileNotFoundError("R&E everywhere_all.json was not found")
    if status.patched:
        return status
    if not status.supported:
        raise ValueError(
            status.detail
            or f"R&E {status.version or 'unknown'} is not supported by this compatibility patch"
        )

    path = status.mission_file
    original, newline, has_bom = _read_rne_text(path)
    patched = _patch_rne_text(original)

    backup = Path(str(path) + RNE_BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)

    tmp = path.with_suffix(path.suffix + ".h145localosm.tmp")
    _write_rne_text(tmp, patched, newline, has_bom)
    try:
        json.loads(tmp.read_text(encoding="utf-8-sig"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

    result = rne_patch_status(community)
    if not result.patched:
        raise ValueError(result.detail or "R&E patch verification failed")
    return result


def restore_rne_mission(community: Path) -> RNEPatchStatus:
    status = rne_patch_status(community)
    if status.mission_file is None:
        raise FileNotFoundError("R&E everywhere_all.json was not found")
    backup = Path(str(status.mission_file) + RNE_BACKUP_SUFFIX)
    if not backup.is_file():
        raise FileNotFoundError("no H145 Local OSM R&E backup exists")

    backup_text = backup.read_text(encoding="utf-8-sig")
    payload = json.loads(backup_text)
    if not isinstance(payload, dict) or _rne_version(payload) is None:
        raise ValueError("R&E backup verification failed")

    shutil.copy2(backup, status.mission_file)
    return rne_patch_status(community)

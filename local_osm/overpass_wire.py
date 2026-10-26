from __future__ import annotations


_TYPE_ORDER = {
    "node": 0,
    "way": 1,
    "relation": 2,
    "area": 3,
}


def canonicalize_elements(elements: list[dict]) -> list[dict]:
    """Return elements in Overpass-compatible type and ID order."""
    return sorted(
        elements,
        key=lambda element: (
            _TYPE_ORDER.get(str(element.get("type", "")), 99),
            str(element.get("type", "")),
            int(element.get("id", 0)),
        ),
    )

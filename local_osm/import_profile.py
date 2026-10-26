from __future__ import annotations

"""H145 Mission System OSM import profile.

The local database intentionally keeps only objects that can be selected by the
known installed H145/HPG mission query corpus, plus topology dependencies and
country boundaries. All tags on retained objects are preserved. The mission
audit command verifies installed ``hpgmission`` JSON files against this profile
so new mission packs cannot silently introduce missing selectors.
"""

from collections.abc import Mapping

PROFILE_NAME = "h145-missions-v1"

# A key in this set is queried for existence (or otherwise requires arbitrary
# values), so every value must be retained.
ANY_VALUE_KEYS = {
    "highway",
    "iata",
    "railway",
}

# Other keys are queried only for these values in the known H145 mission corpus.
EXACT_VALUES: dict[str, set[str]] = {
    "aerialway": {"chair_lift", "gondola"},
    "aeroway": {"aerodrome", "helipad"},
    "amenity": {
        "charging_station",
        "doctors",
        "fire_station",
        "hospital",
        "parking",
        "police",
        "riding_school",
        "school",
    },
    "building": {"apartments", "detached", "hospital", "house", "school", "yes"},
    "cisei": {"bro"},
    "crossing": {"uncontrolled"},
    "emergency": {"ambulance_station", "emergency_ward_entrance", "mountain_rescue"},
    "golf": {"green"},
    "landuse": {"cemetery", "depot", "farmland", "forest", "greenfield", "quarry"},
    "leisure": {"garden", "horse_riding", "pitch", "sports_centre", "stadium"},
    "link": {"bro"},
    "man_made": {"works"},
    "natural": {"cliff", "fell", "glacier", "peak", "saddle", "valley", "water"},
    "place": {"city", "town", "village"},
    "power": {"transformer"},
    "public_transport": {"stop_area"},
    "route": {"piste"},
    "shop": {"supermarket"},
    "social_facility": {"nursing_home"},
    "sport": {"climbing", "equestrian", "free_flying"},
    "tourism": {"camp_site", "hotel"},
    "water": {"lake"},
}

# Native pyosmium KeyFilter pre-filter. It is intentionally slightly broader
# than the final profile so filtering can happen in C++ without changing
# semantics. Objects that pass this key filter are checked again in Python.
DISCOVERY_KEYS = tuple(sorted(ANY_VALUE_KEYS | set(EXACT_VALUES) | {"boundary"}))


def is_country_boundary(tags: Mapping[str, str]) -> bool:
    return (
        tags.get("boundary") == "administrative"
        and tags.get("admin_level") == "2"
    )


def is_relevant_items(tags) -> bool:
    """Test relevance without materializing tags for rejected objects."""
    if isinstance(tags, Mapping):
        if not tags:
            return False
        if is_country_boundary(tags):
            return True
        if any(key in tags for key in ANY_VALUE_KEYS):
            return True
        return any(tags.get(key) in values for key, values in EXACT_VALUES.items())

    boundary_administrative = False
    admin_level_2 = False
    saw_any = False
    for key, value in tags:
        saw_any = True
        key = str(key)
        value = str(value)
        if key in ANY_VALUE_KEYS:
            return True
        values = EXACT_VALUES.get(key)
        if values is not None and value in values:
            return True
        if key == "boundary" and value == "administrative":
            boundary_administrative = True
        elif key == "admin_level" and value == "2":
            admin_level_2 = True

    return saw_any and boundary_administrative and admin_level_2


def is_relevant(tags: Mapping[str, str]) -> bool:
    return is_relevant_items(tags)

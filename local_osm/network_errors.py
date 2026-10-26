"""Consistent network failure reporting for build commands and their GUI host."""

from __future__ import annotations

import urllib.error


NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)
NETWORK_ERROR_PREFIX = "[error] Network request failed:"


def network_failure_message(error: BaseException) -> str:
    return (
        f"{NETWORK_ERROR_PREFIX} {error}. "
        "The remote service may be unavailable or your connection may be interrupted. "
        "Retry later. For database builds, you can also download a .osm.pbf file "
        "from another provider and choose 'Local .osm.pbf files'."
    )

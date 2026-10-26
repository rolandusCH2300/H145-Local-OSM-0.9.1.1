from __future__ import annotations

import json
import urllib.request


def probe_local_server(timeout: float = 0.35) -> bool:
    """Return True only when the expected local H145 Local OSM health endpoint answers."""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8765/health",
            timeout=timeout,
        ) as response:
            if getattr(response, "status", 200) != 200:
                return False
            payload = json.load(response)
    except Exception:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ready"
        and payload.get("service") == "H145 Local OSM"
    )

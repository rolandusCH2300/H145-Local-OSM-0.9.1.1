from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_osm.gui.services import (
    LEGACY_HPG_BACKUP_SUFFIX,
    LOCAL_OVERPASS_NAME,
    LOCAL_OVERPASS_URL,
    OFFICIAL_OVERPASS_NAME,
    OFFICIAL_OVERPASS_URL,
    configure_hpg_connection,
    hpg_connection_status,
    restore_hpg_connection,
)


class HPGLocalOSMLabelTests(unittest.TestCase):
    @staticmethod
    def _write_mission_system(community: Path, *, name: str, url: str) -> Path:
        path = community / "hpg-airbus-h145" / "Config" / "MissionSystem.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "OverpassApiUrls": [
                        {"name": name, "url": url},
                        {"name": "Kumi", "url": "https://example.invalid"},
                    ]
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _first_provider(path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload["OverpassApiUrls"][0]

    def test_configure_renames_provider_and_restore_recovers_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            mission = self._write_mission_system(
                community,
                name=OFFICIAL_OVERPASS_NAME,
                url=OFFICIAL_OVERPASS_URL,
            )

            status = configure_hpg_connection(community)
            self.assertTrue(status.configured)
            self.assertEqual(
                self._first_provider(mission),
                {"name": LOCAL_OVERPASS_NAME, "url": LOCAL_OVERPASS_URL},
            )

            backup = Path(str(mission) + LEGACY_HPG_BACKUP_SUFFIX)
            self.assertEqual(
                self._first_provider(backup),
                {"name": OFFICIAL_OVERPASS_NAME, "url": OFFICIAL_OVERPASS_URL},
            )

            restored = restore_hpg_connection(community)
            self.assertFalse(restored.configured)
            self.assertEqual(
                self._first_provider(mission),
                {"name": OFFICIAL_OVERPASS_NAME, "url": OFFICIAL_OVERPASS_URL},
            )

    def test_legacy_local_url_is_migrated_to_local_osm_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            mission = self._write_mission_system(
                community,
                name=OFFICIAL_OVERPASS_NAME,
                url=LOCAL_OVERPASS_URL,
            )

            self.assertFalse(hpg_connection_status(community).configured)
            status = configure_hpg_connection(community)
            self.assertTrue(status.configured)
            self.assertEqual(
                self._first_provider(mission),
                {"name": LOCAL_OVERPASS_NAME, "url": LOCAL_OVERPASS_URL},
            )


if __name__ == "__main__":
    unittest.main()

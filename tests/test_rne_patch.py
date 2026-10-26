from __future__ import annotations

import codecs
import json
import tempfile
import unittest
from pathlib import Path

from local_osm.gui.rne_patch import (
    RNE_BACKUP_SUFFIX,
    RNE_RELATIVE_PATH,
    _is_patched,
    _patch_rne_text,
    _read_rne_text,
    _write_rne_text,
    find_rne_mission_file,
    rne_patch_status,
)


class RNEPatchTests(unittest.TestCase):
    @staticmethod
    def _write_minimal_mission(community: Path, version: str = "0.997") -> Path:
        path = community / RNE_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "title": f"HEMS RANDOM AND EVERYWHERE MISSIONS {version}",
                    "id": "HEMSRandE",
                    "macros": {
                        "version check": [
                            {
                                "set": {"local": "MISSION_VERSION"},
                                "value": version,
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_finds_rne_relative_to_community_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            path = self._write_minimal_mission(community)
            self.assertEqual(find_rne_mission_file(community), path.resolve())

    def test_supported_unpatched_version_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            path = self._write_minimal_mission(community)
            status = rne_patch_status(community)
            self.assertEqual(status.mission_file, path.resolve())
            self.assertEqual(status.version, "0.997")
            self.assertTrue(status.supported)
            self.assertFalse(status.patched)
            self.assertFalse(status.backup_available)

    def test_backup_availability_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            path = self._write_minimal_mission(community)
            Path(str(path) + RNE_BACKUP_SUFFIX).write_text("backup", encoding="utf-8")
            self.assertTrue(rne_patch_status(community).backup_available)

    def test_unknown_version_is_not_patchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            self._write_minimal_mission(community, "0.998")
            status = rne_patch_status(community)
            self.assertEqual(status.version, "0.998")
            self.assertFalse(status.supported)
            self.assertFalse(status.patched)

    def test_patched_markers_are_recognized(self):
        text = "\n".join(
            [
                '"CICERS PING":[',
                '"DATAQUERYSERVICERANDOM":[',
                '"next_dispatch":[',
                '{"if": {"global":"DATAQUERYSERVICE"}, "eq": 0',
                '{"call_macro": "location reset"}',
                '{"set": {"local":"lock_random_dispatch"}, "value":0}',
                '{"set": {"local":"DISPLAY_DISPATCH"}, "value":1}',
                '"HELOVICTIMS refinement":[',
                '"title":"Local OSM"',
                '"title":"Local OSM"',
                'OSM Service: Local OSM',
                '{"if": {"global": "REALISTIC_DISPATCH"}, "ne":1, "then": [',
            ]
        )
        self.assertTrue(_is_patched(text, "0.997"))

    @staticmethod
    def _patchable_mission(ping_in_ensure_macro: bool, fast_cooldown: object = 10) -> dict:
        ping = {"call_macro": "CICERS PING"}
        links = {"call_macro": "CICERS LINKS"}
        ensure = {"call_macro": "ensure data query service selection"}
        mode = {"set": {"global": "REALISTIC_DISPATCH"}, "value": 0}
        return {
            "id": "HEMSRandE",
            "macros": {
                "version check": [
                    {"set": {"local": "MISSION_VERSION"}, "value": "0.997"}
                ],
                "location reset": [{"sleep": 0.5}],
                "CICERS PING": [
                    {
                        "create_thread": {
                            "commands": [
                                {
                                    "set": {
                                        "var": [
                                            "L:{local:HXX}_PERSIST_MISSION_ENDPOINT_OPTION",
                                            "number",
                                        ]
                                    },
                                    "value": 3,
                                },
                                {"sleep": 0.5},
                            ]
                        }
                    }
                ],
                "next_dispatch": [
                    {
                        "if": {"global": "DATAQUERYSERVICE"},
                        "eq": 3,
                        "then": [{"set": {"local": "cooldowntimer"}, "value": fast_cooldown}],
                        "else": [{"set": {"local": "cooldowntimer"}, "value": 25}],
                    },
                    {"call_macro": "randomize"},
                    {"sleep": 0.5},
                    {"set": {"local": "DISPLAY_DISPATCH"}, "value": 1},
                    {
                        "if": {"local": "randomize_result"},
                        "eq": "ready",
                        "then": [
                            {"set": {"local": "Dispatcher_text"}, "value": ""},
                            {"call_macro": "UpdateRescueTrack"},
                        ],
                    },
                ],
                "settings": [
                    {"buttonbar": [{"title": "Overpass DE"}, {"title": "Kumi"}]},
                    {"buttonbar": [{"title": "Overpass DE"}]},
                    {"text": "OSM Service: Overpass DE"},
                    {
                        "buttonbar": [
                            {"title": "DELAYED (default)"},
                            {"title": "NOT DELAYED", "commands": [mode]},
                        ]
                    },
                ],
                "accident location pregenerator": [
                    {
                        "if": {
                            "and": [
                                {"require": {"local": "CICERS_REMAINING_S"}, "lt": 3600},
                                {"require": {"global": "DATAQUERYSERVICE"}, "eq": 3},
                            ]
                        },
                        "eq": 1,
                        "then": [ping],
                    },
                    {"call_macro": {"local": "Query"}},
                ],
                "objective1": [
                    {
                        "if": {"global": "REALISTIC_DISPATCH"},
                        "eq": None,
                        "then": [{"set": {"global": "REALISTIC_DISPATCH"}, "value": 1}],
                    },
                    *([ensure, links] if ping_in_ensure_macro else [ping, links]),
                    {"sleep": 1},
                ],
                "ensure data query service selection": [{"sleep": 0}]
                + ([ping] if ping_in_ensure_macro else []),
            },
        }

    def test_patch_does_not_depend_on_file_layout(self):
        for ping_in_ensure_macro in (False, True):
            for indent in (None, 1, "\t"):
                with self.subTest(ping_in_ensure_macro=ping_in_ensure_macro, indent=indent):
                    text = json.dumps(
                        self._patchable_mission(ping_in_ensure_macro), indent=indent
                    )
                    self.assertFalse(_is_patched(text, "0.997"))

                    patched = _patch_rne_text(text)
                    self.assertTrue(_is_patched(patched, "0.997"))
                    self.assertEqual(_patch_rne_text(patched), patched)

                    macros = json.loads(patched)["macros"]
                    self.assertNotIn("CICERS LINKS", patched)
                    self.assertEqual(patched.count('"CICERS PING"'), 1)
                    self.assertNotIn("NOT DELAYED", patched)
                    self.assertEqual(
                        macros["next_dispatch"][1:8],
                        [
                            {"call_macro": "location reset"},
                            {"set": {"local": "has_location"}, "value": "no"},
                            {"set": {"local": "has_hospital_location"}, "value": "no"},
                            {"set": {"local": "timer_location"}, "value": None},
                            {"set": {"local": "queryfailed"}, "value": None},
                            {"set": {"local": "query_skipped"}, "value": None},
                            {"call_macro": "randomize"},
                        ],
                    )
                    self.assertEqual(
                        macros["next_dispatch"][-1]["then"][-2:],
                        [
                            {"set": {"local": "lock_random_dispatch"}, "value": 0},
                            {"set": {"local": "DISPLAY_DISPATCH"}, "value": 1},
                        ],
                    )

    def test_patch_accepts_cooldown_read_from_global(self):
        fast = {"global": "NEXT_DISPATCH_COOLDOWN_FAST"}
        text = json.dumps(self._patchable_mission(True, fast), indent=1)

        patched = _patch_rne_text(text)
        self.assertTrue(_is_patched(patched, "0.997"))
        cooldown = json.loads(patched)["macros"]["next_dispatch"][0]
        self.assertEqual(cooldown["eq"], 0)
        self.assertEqual(
            cooldown["else"][0]["then"],
            [{"set": {"local": "cooldowntimer"}, "value": fast}],
        )

    def test_rne_text_io_preserves_lf_and_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "everywhere_all.json"
            original = codecs.BOM_UTF8 + b'{\n  "id":"HEMSRandE"\n}\n'
            path.write_bytes(original)

            text, newline, has_bom = _read_rne_text(path)
            self.assertEqual(newline, "\n")
            self.assertTrue(has_bom)

            _write_rne_text(path, text, newline, has_bom)
            self.assertEqual(path.read_bytes(), original)

    def test_rne_text_io_preserves_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "everywhere_all.json"
            original = b'{\r\n  "id":"HEMSRandE"\r\n}\r\n'
            path.write_bytes(original)

            text, newline, has_bom = _read_rne_text(path)
            self.assertEqual(newline, "\r\n")
            self.assertFalse(has_bom)

            _write_rne_text(path, text, newline, has_bom)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

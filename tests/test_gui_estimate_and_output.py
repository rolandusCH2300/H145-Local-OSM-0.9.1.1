from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from local_osm import database_validation
from local_osm.gui.regions import (
    DB_ESTIMATE_HIGH_MULTIPLIER,
    DB_ESTIMATE_LOW_MULTIPLIER,
    DB_ESTIMATE_MULTIPLIER,
)
from local_osm.gui.user_output import (
    suppress_expected_build_relation_notice,
    suppress_expected_validation_relation_warning,
)


class GuiEstimateAndOutputTests(unittest.TestCase):
    def test_database_estimate_uses_representative_build_range(self) -> None:
        self.assertAlmostEqual(DB_ESTIMATE_MULTIPLIER, 4.375, places=3)
        self.assertAlmostEqual(DB_ESTIMATE_LOW_MULTIPLIER, 3.60, places=2)
        self.assertAlmostEqual(DB_ESTIMATE_HIGH_MULTIPLIER, 5.42, places=2)

    def test_expected_relation_build_warning_is_hidden(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), suppress_expected_build_relation_notice():
            print(
                "[relations] WARNING: 100 referenced relations were not present "
                "after 1 dependency passes"
            )
            print("[relations] nested relation closure complete")

        text = output.getvalue()
        self.assertNotIn("referenced relations were not present", text)
        self.assertIn("nested relation closure complete", text)

    def test_gui_validation_omits_expected_relation_dependency_check(self) -> None:
        expected = SimpleNamespace(
            name="missing_relation_dependencies",
            ok=False,
            severity="warning",
        )
        real_warning = SimpleNamespace(
            name="another_warning",
            ok=False,
            severity="warning",
        )
        report = SimpleNamespace(checks=[expected, real_warning])

        with mock.patch.object(
            database_validation,
            "validate_database",
            return_value=report,
        ):
            with suppress_expected_validation_relation_warning():
                result = database_validation.validate_database("ignored.sqlite3")

        self.assertEqual(
            [check.name for check in result.checks],
            ["another_warning"],
        )


if __name__ == "__main__":
    unittest.main()

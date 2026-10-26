from __future__ import annotations

import builtins
from contextlib import contextmanager
from typing import Iterator


_EXPECTED_RELATION_NOTICE = "referenced relations were not present"
_EXPECTED_VALIDATION_CHECK = "missing_relation_dependencies"


@contextmanager
def suppress_expected_build_relation_notice() -> Iterator[None]:
    """Hide the expected Geofabrik relation-dependency notice in GUI builds.

    The importer keeps tracking the count in import metadata for diagnostics,
    but Geofabrik extracts routinely omit referenced relations outside their
    clipping boundary. Showing this as a warning in the user-facing Build log
    makes a healthy build look suspicious.
    """
    original_print = builtins.print

    def filtered_print(*args, **kwargs):
        text = " ".join(str(value) for value in args)
        if (
            "[relations] WARNING:" in text
            and _EXPECTED_RELATION_NOTICE in text
        ):
            return None
        return original_print(*args, **kwargs)

    builtins.print = filtered_print
    try:
        yield
    finally:
        builtins.print = original_print


@contextmanager
def suppress_expected_validation_relation_warning() -> Iterator[None]:
    """Omit the expected missing-relation check from GUI validation output.

    The raw import statistic remains stored in database metadata. Only the
    user-facing GUI validation report/result is adjusted so this normal extract
    condition does not increment the warning count or print a warning row.
    Command-line validation retains the full diagnostic check.
    """
    from .. import database_validation

    original_validate = database_validation.validate_database

    def validate_without_expected_warning(*args, **kwargs):
        report = original_validate(*args, **kwargs)
        report.checks[:] = [
            check
            for check in report.checks
            if check.name != _EXPECTED_VALIDATION_CHECK
        ]
        return report

    database_validation.validate_database = validate_without_expected_warning
    try:
        yield
    finally:
        database_validation.validate_database = original_validate

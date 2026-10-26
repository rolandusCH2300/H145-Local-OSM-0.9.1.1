from __future__ import annotations

"""Public entry point for schema-v6 imports.

The core owns orchestration, multiprocessing and resource cleanup. The active
discovery, preparation and writing stages are bound from ``tier_c_fast`` before
the importer is exposed to callers.
"""

from . import _importer_v6_base as _base
from .tier_c_fast import install as _install_fast_path

_install_fast_path(_base)

for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)


def _run_main() -> None:
    _base.main()


if __name__ == "__main__":
    _run_main()

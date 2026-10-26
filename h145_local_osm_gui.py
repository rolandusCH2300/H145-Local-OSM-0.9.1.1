from __future__ import annotations


if __name__ == "__main__":
    # PyInstaller multiprocessing workers re-enter this executable on Windows.
    # Divert those worker invocations before importing the GUI/task modules so
    # a spawned topology worker can never fall through into a second GUI.
    import multiprocessing

    multiprocessing.freeze_support()

    from local_osm.gui.__main__ import main

    raise SystemExit(main())

"""Portable desktop GUI for H145 Local OSM.

PySide6 is intentionally not imported here so the service layer and normal CLI
remain usable without GUI dependencies installed.
"""

from . import services as services
from .probe import probe_local_server

# Keep the probe available through the service namespace used by the GUI while
# leaving the Qt-free service module independently testable.
services.probe_local_server = probe_local_server

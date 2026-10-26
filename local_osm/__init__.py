__version__ = "0.999.5.5"

# Schema v6 keeps only searchable selector tags normalized in object_tags. Make
# secondary predicates use the authoritative packed full-tag payload instead of
# treating omitted metadata as a negative match. Install this inner adapter first
# so rejected candidates never reach the geographic coverage checks.
from .selector_filter_runtime import install_selector_filter_runtime as _install_selector_filter_runtime

_install_selector_filter_runtime()
del _install_selector_filter_runtime

# Coverage wraps the complete candidate filter before query/server symbols are
# imported. It retains the same exact root checks and intersection-node rules.
from .coverage_runtime import install_coverage_runtime as _install_coverage_runtime

_install_coverage_runtime()
del _install_coverage_runtime

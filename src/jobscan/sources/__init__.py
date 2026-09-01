"""Source plugins.

Importing this package registers every built-in source and then loads any
third-party ones advertised under the `jobscan.sources` entry-point group.
"""

from __future__ import annotations

from jobscan.sources import (  # noqa: F401 - import registers
    adzuna,
    ats,
    structured,
    workday,
)
from jobscan.sources.base import (
    Source,
    available,
    get_source,
    load_plugins,
    register,
    strip_html,
)

load_plugins()

__all__ = [
    "Source",
    "available",
    "get_source",
    "load_plugins",
    "register",
    "strip_html",
]

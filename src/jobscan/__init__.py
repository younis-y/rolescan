"""jobscan: a typed, async, plugin-based personal job scanner."""

from __future__ import annotations

from jobscan.config import Config
from jobscan.models import CVVariant, FitVerdict, Job, ScoredJob, Verdict
from jobscan.pipeline import ScanResult, run_scan

__version__ = "2.2.0"

__all__ = [
    "CVVariant",
    "Config",
    "FitVerdict",
    "Job",
    "ScanResult",
    "ScoredJob",
    "Verdict",
    "__version__",
    "run_scan",
]

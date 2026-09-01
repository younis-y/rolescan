"""The scan pipeline.

    fetch all sources concurrently
      -> deduplicate within the run
      -> keyword score
      -> drop anything already reported
      -> prefilter to plausible roles
      -> LLM fit score and CV match (cached)
      -> record and rank

Everything here is orchestration. The judgement lives in scoring, the IO in
sources and store.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from jobscan.config import Config, SourceEntry
from jobscan.http import Fetcher
from jobscan.models import Job, ScoredJob
from jobscan.scoring import CVLibrary, FitScorer, score_keywords
from jobscan.sources import get_source
from jobscan.sources.base import PostingCache, SourceSkipped
from jobscan.store import Store

__all__ = ["ScanResult", "SourceReport", "run_scan"]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SourceReport:
    kind: str
    slug: str
    label: str
    count: int = 0
    error: str = ""
    skipped: bool = False
    """Declined to run (no credentials, unsupported region). Not a failure,
    but not a success either: nothing was tested."""

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped


@dataclass(slots=True)
class ScanResult:
    reports: list[SourceReport] = field(default_factory=list)
    fetched: int = 0
    unique: int = 0
    already_seen: int = 0
    prefiltered: int = 0
    llm_calls: int = 0
    llm_cached: int = 0
    llm_errors: int = 0
    llm_error_detail: str = ""
    reportable: list[ScoredJob] = field(default_factory=list)
    dry_run: bool = False

    @property
    def failed_sources(self) -> list[SourceReport]:
        return [r for r in self.reports if r.error and not r.skipped]

    @property
    def skipped_sources(self) -> list[SourceReport]:
        return [r for r in self.reports if r.skipped]


async def _fetch_one(
    entry: SourceEntry, fetcher: Fetcher, cache: PostingCache | None = None
) -> tuple[SourceReport, list[Job]]:
    report = SourceReport(kind=entry.kind, slug=entry.slug, label=entry.label)
    try:
        source = get_source(entry, fetcher, cache)
        jobs = await source.fetch()
    except SourceSkipped as e:
        report.skipped = True
        report.error = str(e)
        log.info("source %s/%s skipped: %s", entry.kind, entry.slug, e)
        return report, []
    except Exception as e:
        # One dead board must never take down the scan. This is the single most
        # important error boundary in the tool: forty sources means forty
        # chances for a 404 or a schema change.
        report.error = f"{type(e).__name__}: {e}"
        log.warning("source %s/%s failed: %s", entry.kind, entry.slug, report.error)
        return report, []
    report.count = len(jobs)
    return report, jobs


async def fetch_all(
    cfg: Config, cache: PostingCache | None = None
) -> tuple[list[SourceReport], list[Job]]:
    entries = cfg.enabled_sources
    if not entries:
        return [], []
    async with Fetcher(cfg.http) as fetcher:
        pairs = await asyncio.gather(*(_fetch_one(e, fetcher, cache) for e in entries))
    reports = [p[0] for p in pairs]
    jobs = [j for p in pairs for j in p[1]]
    return reports, jobs


def deduplicate(jobs: list[Job]) -> list[Job]:
    """Collapse the same role appearing on two boards.

    Keeps the richest copy: an aggregator hit and a direct ATS hit for one role
    should resolve to the ATS one, which has the full description and the real
    apply link.
    """
    best: dict[str, Job] = {}
    for job in jobs:
        current = best.get(job.uid)
        if current is None or len(job.description) > len(current.description):
            best[job.uid] = job
    return list(best.values())


async def run_scan(cfg: Config, *, dry_run: bool = False) -> ScanResult:
    result = ScanResult(dry_run=dry_run)

    db_path = cfg.resolve(cfg.output.db_path)
    # The store opens BEFORE fetching, not after: the structured source needs
    # the posting cache during fetch to skip detail pages whose sitemap lastmod
    # has not moved. Opening it afterwards would leave that cache write-only.
    async with Store(db_path) as store:
        reports, raw = await fetch_all(cfg, store)
        result.reports = reports
        result.fetched = len(raw)

        unique = deduplicate(raw)
        result.unique = len(unique)

        scored = [score_keywords(j, cfg.profile) for j in unique]

        fresh = await store.filter_new(scored)
        result.already_seen = len(scored) - len(fresh)

        candidates = [
            s for s in fresh if s.keyword_score >= cfg.profile.min_keyword_score
        ]
        result.prefiltered = len(fresh) - len(candidates)

        cvs = CVLibrary.load(
            cfg.resolve(cfg.profile.cv_dir) if cfg.profile.cv_dir else None
        )
        scorer = FitScorer(cfg.llm, cfg.profile, cvs, store)
        judged = await scorer.score_all(candidates)
        result.llm_calls = scorer.calls_made
        result.llm_cached = sum(1 for s in judged if s.llm_cached)
        result.llm_errors = scorer.errors
        result.llm_error_detail = scorer.first_error

        if not dry_run:
            # Record every fresh posting, not only the reported ones, so a role
            # rejected today is not re-surfaced tomorrow.
            by_uid = {s.job.uid: s for s in fresh}
            by_uid.update({s.job.uid: s for s in judged})
            await store.record_all(list(by_uid.values()))

    keep = [s for s in judged if s.score >= cfg.profile.min_report_score]
    if not cfg.output.show_blocked:
        keep = [s for s in keep if not s.is_blocked]
    keep.sort(key=lambda s: s.sort_key(), reverse=True)
    result.reportable = keep[: cfg.output.max_roles]
    return result

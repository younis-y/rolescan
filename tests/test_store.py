from __future__ import annotations

from pathlib import Path

from jobscan.models import (
    Confidence,
    CVVariant,
    FitVerdict,
    Job,
    ScoredJob,
    Verdict,
)
from jobscan.store import Store

VERDICT = FitVerdict(
    fit_score=88,
    verdict=Verdict.APPLY,
    confidence=Confidence.HIGH,
    reason="Strong overlap with the day-ahead forecasting project.",
    cv_variant=CVVariant.ENERGY,
    tailoring=["Lead with the LP battery dispatch result."],
)


async def test_roundtrip_and_dedup(tmp_path: Path, energy_job: Job) -> None:
    db = tmp_path / "seen.db"
    scored = ScoredJob(job=energy_job, keyword_score=40)
    async with Store(db) as store:
        assert await store.is_new(energy_job)
        await store.record_all([scored])
        assert not await store.is_new(energy_job)
        assert await store.count() == 1

    async with Store(db) as store:
        assert not await store.is_new(energy_job), "state must survive reopening"


async def test_filter_new_partitions_in_one_pass(
    tmp_path: Path, energy_job: Job, gated_job: Job
) -> None:
    a, b = ScoredJob(job=energy_job), ScoredJob(job=gated_job)
    async with Store(tmp_path / "s.db") as store:
        await store.record_all([a])
        fresh = await store.filter_new([a, b])
        assert [s.job.uid for s in fresh] == [b.job.uid]


async def test_record_is_idempotent(tmp_path: Path, energy_job: Job) -> None:
    async with Store(tmp_path / "s.db") as store:
        await store.record_all([ScoredJob(job=energy_job, keyword_score=10)])
        await store.record_all([ScoredJob(job=energy_job, keyword_score=90)])
        assert await store.count() == 1


async def test_verdict_cache_roundtrip(tmp_path: Path, energy_job: Job) -> None:
    async with Store(tmp_path / "s.db") as store:
        assert await store.get_verdict(energy_job.content_hash, 30) is None
        await store.put_verdict(energy_job.content_hash, VERDICT)
        got = await store.get_verdict(energy_job.content_hash, 30)
        assert got is not None
        assert got.fit_score == 88
        assert got.cv_variant is CVVariant.ENERGY
        assert got.tailoring == VERDICT.tailoring


async def test_edited_posting_misses_the_cache(tmp_path: Path, energy_job: Job) -> None:
    edited = energy_job.model_copy(update={"description": "rewritten posting"})
    async with Store(tmp_path / "s.db") as store:
        await store.put_verdict(energy_job.content_hash, VERDICT)
        assert await store.get_verdict(edited.content_hash, 30) is None


async def test_corrupt_cache_row_is_dropped_not_raised(
    tmp_path: Path, energy_job: Job
) -> None:
    """A schema change must not brick the tool."""
    async with Store(tmp_path / "s.db") as store:
        await store.db.execute(
            "INSERT INTO verdicts VALUES (?,?,?)",
            (
                energy_job.content_hash,
                '{"nonsense": true}',
                "2026-08-01T00:00:00+00:00",
            ),
        )
        await store.db.commit()
        assert await store.get_verdict(energy_job.content_hash, 30) is None
        assert await store.get_verdict(energy_job.content_hash, 30) is None


async def test_prune_drops_old_verdicts(tmp_path: Path, energy_job: Job) -> None:
    async with Store(tmp_path / "s.db") as store:
        await store.db.execute(
            "INSERT INTO verdicts VALUES (?,?,?)",
            (
                energy_job.content_hash,
                VERDICT.model_dump_json(),
                "2020-01-01T00:00:00+00:00",
            ),
        )
        await store.db.commit()
        assert await store.prune(days=30) == 1
        assert await store.get_verdict(energy_job.content_hash, 30) is None


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    for _ in range(3):
        async with Store(db) as store:
            assert await store.count() == 0


# --- posting cache: sitemap lastmod is the only invalidation signal --------
# Verified 2026-08-25 that ADNOC and ACWA Power ignore If-Modified-Since and
# return 200 with the full body, so conditional GETs cannot be used and the
# sitemap's lastmod is what gates a refetch.


def _job(url: str, title: str = "Analyst") -> Job:
    return Job(source="structured", company="ADNOC", title=title, url=url)


async def test_posting_cache_miss_returns_none(tmp_path: Path) -> None:
    async with Store(tmp_path / "s.db") as store:
        assert await store.get_posting("https://x/job/1") is None


async def test_posting_cache_round_trips_lastmod_and_job(tmp_path: Path) -> None:
    job = _job("https://x/job/1", "Senior Analyst")
    async with Store(tmp_path / "s.db") as store:
        await store.put_posting("https://x/job/1", "2026-08-25", job)
        hit = await store.get_posting("https://x/job/1")
    assert hit is not None
    lastmod, cached = hit
    assert lastmod == "2026-08-25"
    assert cached.title == "Senior Analyst"
    assert cached.uid == job.uid, "the cached job must keep its identity"


async def test_posting_cache_overwrites_on_a_new_lastmod(tmp_path: Path) -> None:
    async with Store(tmp_path / "s.db") as store:
        await store.put_posting(
            "https://x/job/1", "2026-08-01", _job("https://x/job/1")
        )
        await store.put_posting(
            "https://x/job/1", "2026-08-25", _job("https://x/job/1", "Retitled")
        )
        hit = await store.get_posting("https://x/job/1")
    assert hit is not None
    assert hit[0] == "2026-08-25"
    assert hit[1].title == "Retitled"


async def test_unreadable_cached_posting_is_dropped_not_raised(
    tmp_path: Path,
) -> None:
    """A model change must degrade to a refetch, never crash the scan."""
    async with Store(tmp_path / "s.db") as store:
        await store.put_posting(
            "https://x/job/1", "2026-08-25", _job("https://x/job/1")
        )
        await store.db.execute(
            "UPDATE postings SET payload='{not json' WHERE url=?", ("https://x/job/1",)
        )
        assert await store.get_posting("https://x/job/1") is None


async def test_posting_cache_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    async with Store(path) as store:
        await store.put_posting(
            "https://x/job/1", "2026-08-25", _job("https://x/job/1")
        )
    async with Store(path) as store:
        assert await store.get_posting("https://x/job/1") is not None

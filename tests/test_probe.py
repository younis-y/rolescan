"""Probe classification.

Regression suite for the false-OK bug: `discover` used to classify on
`count >= 0`, so a SmartRecruiters slug that does not exist (HTTP 200,
totalFound=0) reported as working, and Adzuna reported as working when it had
never run at all.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from rolescan.config import HTTPConfig, SourceEntry
from rolescan.http import Fetcher
from rolescan.sources import get_source
from rolescan.sources.base import ProbeResult, ProbeStatus, SourceSkipped

SR = "https://api.smartrecruiters.com/v1/companies/{}/postings"
GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"


async def _probe(entry: SourceEntry) -> ProbeResult:
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        return await get_source(entry, f).probe()


@respx.mock
async def test_smartrecruiters_zero_is_unknown_not_ok() -> None:
    """The exact bug: a nonsense slug answers 200 with totalFound=0."""
    respx.get(SR.format("ThisCompanyDoesNotExistXYZ123")).mock(
        return_value=httpx.Response(200, json={"totalFound": 0, "content": []})
    )
    result = await _probe(
        SourceEntry(kind="smartrecruiters", slug="ThisCompanyDoesNotExistXYZ123")
    )
    assert result.status is ProbeStatus.UNKNOWN
    assert not result.trustworthy, "an unverifiable slug must not count as working"


@respx.mock
async def test_smartrecruiters_with_jobs_is_ok() -> None:
    respx.get(SR.format("Masdar")).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {
                        "id": "1",
                        "name": "Data Scientist",
                        "location": {"city": "Abu Dhabi", "country": "UAE"},
                    }
                ],
            },
        )
    )
    result = await _probe(SourceEntry(kind="smartrecruiters", slug="Masdar"))
    assert result.status is ProbeStatus.OK
    assert result.count == 1
    assert result.trustworthy


@respx.mock
async def test_greenhouse_zero_is_empty_not_unknown() -> None:
    """Greenhouse 404s unknown slugs, so a 200 with zero jobs is trustworthy:
    the board exists and simply has no openings."""
    respx.get(GH.format("realcompany")).mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    result = await _probe(SourceEntry(kind="greenhouse", slug="realcompany"))
    assert result.status is ProbeStatus.EMPTY
    assert result.trustworthy, "a 404-ing API makes an empty board verifiable"


@respx.mock
async def test_bad_slug_on_a_404ing_api_is_fail() -> None:
    respx.get(GH.format("nope")).mock(return_value=httpx.Response(404))
    result = await _probe(SourceEntry(kind="greenhouse", slug="nope"))
    assert result.status is ProbeStatus.FAIL
    assert "404" in result.detail
    assert not result.trustworthy


async def test_adzuna_without_credentials_is_skipped_not_ok() -> None:
    """Second false OK: returning [] read as a successful zero-result probe."""
    result = await _probe(SourceEntry(kind="adzuna", slug="gb", queries=["energy"]))
    assert result.status is ProbeStatus.SKIPPED
    assert not result.trustworthy
    assert "credentials" in result.detail


async def test_adzuna_unsupported_country_is_skipped() -> None:
    result = await _probe(
        SourceEntry(kind="adzuna", slug="ae", app_id="x", app_key="y", queries=["e"])
    )
    assert result.status is ProbeStatus.SKIPPED
    assert "UAE" in result.detail


async def test_adzuna_raises_source_skipped_from_fetch() -> None:
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        source = get_source(SourceEntry(kind="adzuna", slug="gb"), f)
        with pytest.raises(SourceSkipped):
            await source.fetch()


@respx.mock
async def test_workday_422_means_the_tenant_does_not_exist() -> None:
    """Verified against the live API on 2026-08-24:

        tenant=adnoc                 bogus site -> 422
        tenant=zzznonsensetenant999  bogus site -> 422
        tenant=centrica              bogus site -> 404

    `*.myworkdayjobs.com` is a wildcard record, so any hostname connects and
    Workday answers 422 when no tenant sits behind it. The fix is the slug or
    the host, never the site.
    """
    base = "https://adnoc.wd3.myworkdayjobs.com/wday/cxs/adnoc/WrongSite"
    respx.post(f"{base}/jobs").mock(return_value=httpx.Response(422))
    result = await _probe(
        SourceEntry(kind="workday", slug="adnoc", site="WrongSite", host="wd3")
    )
    assert result.status is ProbeStatus.FAIL
    assert "422" in result.detail
    assert "adnoc" in result.detail, "name the tenant that does not exist"
    assert "site" not in result.detail.lower(), "a 422 is not a site problem"


@respx.mock
async def test_workday_404_points_at_the_site_value() -> None:
    """A 404 is the closer miss: the tenant exists and rejected the site."""
    base = "https://centrica.wd3.myworkdayjobs.com/wday/cxs/centrica/WrongSite"
    respx.post(f"{base}/jobs").mock(return_value=httpx.Response(404))
    result = await _probe(
        SourceEntry(kind="workday", slug="centrica", site="WrongSite", host="wd3")
    )
    assert result.status is ProbeStatus.FAIL
    assert "404" in result.detail
    assert "WrongSite" in result.detail
    assert "site" in result.detail.lower()
    assert "/en-US/" in result.detail, "say where to read the real site value"


def test_only_smartrecruiters_is_marked_ambiguous() -> None:
    from rolescan.sources import available

    ambiguous = {n for n, c in available().items() if c.ambiguous_when_empty}
    assert ambiguous == {"smartrecruiters"}

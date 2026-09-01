"""The generic schema.org source.

Verified against the live sites on 2026-08-25:
  ADNOC (Phenom)              JSON-LD  <script type="application/ld+json">
  ACWA Power (SuccessFactors) MICRODATA itemscope/itemprop, no JSON-LD at all
Both server-render, so no browser is needed. Neither honours
If-Modified-Since -- both answer 200 with the full body -- so the sitemap's
lastmod is the only usable invalidation signal.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobscan.config import HTTPConfig, SourceEntry
from jobscan.http import Fetcher
from jobscan.models import Job
from jobscan.sources import get_source
from jobscan.sources.base import ProbeStatus
from jobscan.store import Store

SITEMAP_URL = "https://jobs.example.ae/sitemap.xml"
JOB1 = "https://jobs.example.ae/us/en/job/32712/senior-analyst"
JOB2 = "https://jobs.example.ae/us/en/job/32718/engineer-contracts"
NOT_A_JOB = "https://jobs.example.ae/us/en/scam-alert-page"


def sitemap(*entries: tuple[str, str]) -> str:
    urls = "".join(
        f"<url><loc>{loc}</loc><lastmod>{lm}</lastmod></url>" for loc, lm in entries
    )
    return f'<?xml version="1.0"?><urlset>{urls}</urlset>'


# ADNOC's description arrives as HTML escaped INSIDE a JSON string inside a
# <script> tag, so the raw bytes carry &lt;p&gt; rather than <p>.
JSONLD_PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"JobPosting",
 "title":"Senior Analyst, Investor Relations",
 "datePosted":"2026-08-23",
 "employmentType":["OTHER","INTERN"],
 "identifier":{"@type":"PropertyValue","name":"ADNOC","value":"32712"},
 "hiringOrganization":{"@type":"Organization","name":"ADNOC Distribution"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Abu Dhabi","addressCountry":"United Arab Emirates"}},
 "description":"&lt;p&gt;&lt;strong&gt;JOB PURPOSE&lt;/strong&gt;&lt;/p&gt;&lt;p&gt;Collect and analyse investor data.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;8 years experience&lt;/li&gt;&lt;/ul&gt;"}
</script></head><body>page</body></html>"""

MICRODATA_PAGE = """<html><body>
<div itemscope itemtype="http://schema.org/JobPosting">
  <h1 itemprop="title">Project Coordinator</h1>
  <meta itemprop="datePosted" content="2026-08-22">
  <meta itemprop="validThrough" content="2026-08-26">
  <meta itemprop="hiringOrganization" content="ACWA Power">
  <span itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">
    <span itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">
      <meta itemprop="addressLocality" content="Riyadh">
    </span>
  </span>
  <span itemprop="description">Coordinate site projects and reporting.</span>
</div></body></html>"""

NO_MARKUP_PAGE = "<html><body><h1>Just a page</h1></body></html>"


def entry(**over: object) -> SourceEntry:
    base: dict[str, object] = {
        "kind": "structured",
        "slug": "example",
        "label": "Example Energy",
        "sitemap": SITEMAP_URL,
        "url_pattern": "/job/",
        "delay": 0,
    }
    base.update(over)
    return SourceEntry(**base)


async def fetch(e: SourceEntry, cache: Store | None = None) -> list[Job]:
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        return await get_source(e, f, cache=cache).fetch()


# --- discovery -------------------------------------------------------------


@respx.mock
async def test_only_urls_matching_the_pattern_are_fetched() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(
            200, text=sitemap((JOB1, "2026-08-25"), (NOT_A_JOB, "2026-08-25"))
        )
    )
    job_route = respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    skip_route = respx.get(NOT_A_JOB).mock(return_value=httpx.Response(200, text=""))
    jobs = await fetch(entry())
    assert len(jobs) == 1
    assert job_route.call_count == 1
    assert skip_route.call_count == 0, "non-job URLs must never be fetched"


@respx.mock
async def test_empty_sitemap_probes_as_empty_not_unknown() -> None:
    respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=sitemap()))
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        result = await get_source(entry(), f).probe()
    assert result.status is ProbeStatus.EMPTY


@respx.mock
async def test_a_page_without_markup_is_skipped_not_fatal() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x"), (JOB2, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=NO_MARKUP_PAGE))
    respx.get(JOB2).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    jobs = await fetch(entry())
    assert len(jobs) == 1, "one unmarked page must not lose the other postings"


@respx.mock
async def test_a_dead_detail_page_does_not_abort_the_source() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x"), (JOB2, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(404))
    respx.get(JOB2).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    jobs = await fetch(entry())
    assert len(jobs) == 1


# --- parsing, both syntaxes ------------------------------------------------


@respx.mock
async def test_parses_json_ld() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-25")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    job = (await fetch(entry()))[0]
    assert job.title == "Senior Analyst, Investor Relations"
    assert job.company == "ADNOC Distribution"
    assert "Abu Dhabi" in job.location
    assert str(job.posted) == "2026-08-23"
    assert job.url == JOB1
    assert job.raw_id == "32712"


@respx.mock
async def test_parses_microdata_when_there_is_no_json_ld() -> None:
    """ACWA Power has no JSON-LD at all. Asking only for JSON-LD would have
    reported this employer as unscrapeable."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-22")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=MICRODATA_PAGE))
    job = (await fetch(entry()))[0]
    assert job.title == "Project Coordinator"
    assert job.company == "ACWA Power"
    assert "Riyadh" in job.location
    assert str(job.posted) == "2026-08-22"
    assert "Coordinate site projects" in job.description


@respx.mock
async def test_description_is_unescaped_to_plain_text() -> None:
    """The LLM scorer must never receive literal &lt;br&gt; or raw tags."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    desc = (await fetch(entry()))[0].description
    assert "JOB PURPOSE" in desc
    assert "8 years experience" in desc
    assert "&lt;" not in desc and "&gt;" not in desc and "&amp;" not in desc
    assert "<p>" not in desc and "<strong>" not in desc and "<" not in desc


@respx.mock
async def test_employment_type_is_ignored_entirely() -> None:
    """["OTHER","INTERN"] on a senior role means the field is noise."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    job = (await fetch(entry()))[0]
    blob = job.model_dump_json().casefold()
    assert "intern" not in blob, "employmentType must not leak into the job"


@respx.mock
async def test_missing_valid_through_is_not_required() -> None:
    """ADNOC omits validThrough; ACWA has it. Neither may be mandatory."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    assert len(await fetch(entry())) == 1


# --- the lastmod cache -----------------------------------------------------


@respx.mock
async def test_unchanged_lastmod_serves_from_cache_without_refetching(
    tmp_path: Path,
) -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-25")))
    )
    route = respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    async with Store(tmp_path / "s.db") as store:
        first = await fetch(entry(), cache=store)
        assert route.call_count == 1
        second = await fetch(entry(), cache=store)
    assert route.call_count == 1, "an unchanged lastmod must not re-fetch 1.25MB"
    assert [j.uid for j in second] == [j.uid for j in first]
    assert second[0].title == first[0].title


@respx.mock
async def test_a_moved_lastmod_refetches(tmp_path: Path) -> None:
    route = respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    async with Store(tmp_path / "s.db") as store:
        respx.get(SITEMAP_URL).mock(
            return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-25")))
        )
        await fetch(entry(), cache=store)
        respx.get(SITEMAP_URL).mock(
            return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-26")))
        )
        await fetch(entry(), cache=store)
    assert route.call_count == 2


@respx.mock
async def test_cached_postings_are_still_emitted(tmp_path: Path) -> None:
    """A cache hit must still yield the job. Dropping it would make a posting
    that was fetched but never recorded -- a --dry run, or a crash before
    record_all -- invisible for ever, because its lastmod never moves again."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-25")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    async with Store(tmp_path / "s.db") as store:
        await fetch(entry(), cache=store)
        second = await fetch(entry(), cache=store)
    assert len(second) == 1, "an unchanged posting must still reach the pipeline"


@respx.mock
async def test_works_with_no_cache_configured() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-25")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    assert len(await fetch(entry(), cache=None)) == 1


# --- politeness ------------------------------------------------------------


@respx.mock
async def test_delay_is_applied_between_detail_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("jobscan.sources.structured.asyncio.sleep", fake_sleep)
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x"), (JOB2, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    respx.get(JOB2).mock(return_value=httpx.Response(200, text=MICRODATA_PAGE))
    await fetch(entry(delay=0.4))
    assert slept == [0.4], "one delay between two fetches, not before the first"


# --- defects found by running against the live sites, 2026-08-25 -----------


ACWA_SHAPED = """<html><body>
<div itemscope itemtype="http://schema.org/JobPosting">
  <h1 itemprop="title">Project Coordinator</h1>
  <meta itemprop="datePosted" content="Wed Aug 05 00:00:00 UTC 2026">
  <meta itemprop="validThrough" content="Wed Aug 26 18:30:00 UTC 2026">
  <meta itemprop="hiringOrganization" content="ACWA Power">
  <span itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">
    <span itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">
      <meta itemprop="streetAddress" content="SA">
    </span>
  </span>
  <span itemprop="description">Coordinate site projects.</span>
</div></body></html>"""


@respx.mock
async def test_street_address_is_used_when_it_is_the_only_location() -> None:
    """ACWA Power publishes nothing but streetAddress. Ignoring it left every
    posting with an empty location, which the location penalty then punishes.
    The bare code it contains is expanded separately, see the country tests."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-22")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=ACWA_SHAPED))
    job = (await fetch(entry()))[0]
    assert job.location, "streetAddress is the only location ACWA Power publishes"
    assert job.location == "Saudi Arabia"


@respx.mock
async def test_java_style_date_posted_is_parsed() -> None:
    """SuccessFactors emits Java's Date.toString, which is not ISO and which
    Job._parse_date silently discards, leaving posted=None on every posting."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "2026-08-22")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=ACWA_SHAPED))
    job = (await fetch(entry()))[0]
    assert str(job.posted) == "2026-08-05"


@respx.mock
async def test_an_unparseable_date_is_dropped_not_fatal() -> None:
    page = ACWA_SHAPED.replace("Wed Aug 05 00:00:00 UTC 2026", "sometime soon")
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=page))
    job = (await fetch(entry()))[0]
    assert job.posted is None
    assert job.title == "Project Coordinator"


# --- probe must not download the whole board -------------------------------


@respx.mock
async def test_probe_reads_the_sitemap_and_one_sample_not_every_page() -> None:
    """`probe` inherited from Source calls fetch(), which for this source means
    every detail page: 170 requests and ~212MB for ADNOC, just to run
    `jobscan discover`. It needs the sitemap plus one sample, and no more."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(
            200, text=sitemap((JOB1, "x"), (JOB2, "x"), (NOT_A_JOB, "x"))
        )
    )
    first = respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    second = respx.get(JOB2).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        result = await get_source(entry(), f).probe()
    assert result.status is ProbeStatus.OK
    assert result.count == 2, "count comes from the sitemap, not from downloading"
    assert first.call_count == 1, "exactly one sample page"
    assert second.call_count == 0, "the rest must not be touched"


@respx.mock
async def test_probe_flags_a_pattern_that_matches_nothing() -> None:
    """A sitemap full of URLs and none matching is a wrong url_pattern, which
    is a different problem from an employer with no vacancies."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((NOT_A_JOB, "x")))
    )
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        result = await get_source(entry(url_pattern="/vacancy/"), f).probe()
    assert result.status is ProbeStatus.UNKNOWN
    assert "/vacancy/" in result.detail


@respx.mock
async def test_probe_flags_a_sample_page_carrying_no_markup() -> None:
    """URLs in the sitemap prove nothing if the pages have no JobPosting."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=NO_MARKUP_PAGE))
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        result = await get_source(entry(), f).probe()
    assert result.status is ProbeStatus.UNKNOWN
    assert "markup" in result.detail.casefold()


@respx.mock
async def test_probe_reports_an_unreachable_sitemap_as_fail() -> None:
    respx.get(SITEMAP_URL).mock(return_value=httpx.Response(404))
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        result = await get_source(entry(), f).probe()
    assert result.status is ProbeStatus.FAIL


# --- bare country codes ----------------------------------------------------


@respx.mock
async def test_a_bare_country_code_is_expanded_to_the_country_name() -> None:
    """ACWA Power's only location field is streetAddress="SA". The profile
    lists "saudi", so "SA" scores a location mismatch on every posting. It
    cannot be fixed by adding "sa" to the profile either: _location_ok does a
    substring test, so "sa" would match "usa" and "Sale" too."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=ACWA_SHAPED))
    job = (await fetch(entry()))[0]
    assert job.location == "Saudi Arabia"


@respx.mock
async def test_a_real_place_name_is_never_rewritten() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    assert (await fetch(entry()))[0].location == "Abu Dhabi, United Arab Emirates"


@respx.mock
async def test_an_unknown_two_letter_code_is_left_alone() -> None:
    page = ACWA_SHAPED.replace('content="SA"', 'content="ZZ"')
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=page))
    assert (await fetch(entry()))[0].location == "ZZ"


# --- hiringOrganization is sometimes an internal tenant id -----------------


@respx.mock
async def test_an_identifier_shaped_org_name_falls_back_to_the_label() -> None:
    """National Grid's SuccessFactors instance reports hiringOrganization as
    "natgridProd", its tenant id. That went straight into the digest as the
    employer name for all 161 postings."""
    page = MICRODATA_PAGE.replace('content="ACWA Power"', 'content="natgridProd"')
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=page))
    job = (await fetch(entry(label="National Grid")))[0]
    assert job.company == "National Grid"


@respx.mock
async def test_a_real_subsidiary_name_is_kept_over_the_label() -> None:
    """ADNOC reports "ADNOC Logistics & Services", which is more useful than
    the configured label and must survive."""
    page = JSONLD_PAGE.replace("ADNOC Distribution", "ADNOC Logistics & Services")
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=page))
    job = (await fetch(entry(label="ADNOC")))[0]
    assert job.company == "ADNOC Logistics & Services"


# --- optional exclusion, for boards that mix regions -----------------------


@respx.mock
async def test_exclude_pattern_skips_urls_without_fetching_them() -> None:
    """National Grid runs UK and US hiring on one SuccessFactors instance:
    108 of 164 sitemap URLs are US roles that the profile's location list
    correctly penalises anyway. Fetching them is pure waste."""
    us = "https://jobs.example.ae/us/en/job/9001/analyst-MA-01610"
    uk = "https://jobs.example.ae/us/en/job/9002/analyst-NW8-8LB"
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((us, "x"), (uk, "x")))
    )
    us_route = respx.get(us).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    uk_route = respx.get(uk).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    jobs = await fetch(entry(exclude_pattern=r"-[A-Z]{2}-\d{5}$"))
    assert len(jobs) == 1
    assert us_route.call_count == 0, "excluded urls must never be requested"
    assert uk_route.call_count == 1


@respx.mock
async def test_no_exclude_pattern_changes_nothing() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x"), (JOB2, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    respx.get(JOB2).mock(return_value=httpx.Response(200, text=MICRODATA_PAGE))
    assert len(await fetch(entry())) == 2


@respx.mock
async def test_probe_counts_only_included_urls() -> None:
    """discover must report the count the scan will actually fetch."""
    us = "https://jobs.example.ae/us/en/job/9001/analyst-MA-01610"
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((us, "x"), (JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    async with Fetcher(HTTPConfig(max_retries=0)) as f:
        result = await get_source(entry(exclude_pattern=r"-[A-Z]{2}-\d{5}$"), f).probe()
    assert result.status is ProbeStatus.OK
    assert result.count == 1


# --- thin markup: fall back to the page body ------------------------------


SKELETON_PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"JobPosting",
 "title":"Senior Reservoir Engineer",
 "datePosted":"2026-08-10 15:37:39",
 "description":null,
 "hiringOrganization":{"@type":"Organization","name":"Taqa"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":null}}}
</script>
<style>.nav{color:red}</style></head>
<body><nav>Home Vacancies Profile</nav>
<div id="content"><p>Ref: TNO-RQ26-37. Location: Calgary, AB. Canada.</p>
<p>The Senior Reservoir Engineer will build reservoir simulation models and
run production forecasting across the asset base, using Python and SQL.</p></div>
<script>var tracking = "should not appear";</script>
<footer>Copyright 2026</footer></body></html>"""


@respx.mock
async def test_empty_json_ld_description_falls_back_to_the_page_body() -> None:
    """TAQA's Harbour ATS emits JobPosting with description:null while the real
    text sits in the HTML body. Taking the markup at face value gives every
    posting an empty description, a keyword score of zero, and silent removal
    at the prefilter — the source looks configured and returns nothing."""
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=SKELETON_PAGE))
    job = (await fetch(entry()))[0]
    assert "reservoir simulation models" in job.description
    assert "production forecasting" in job.description
    assert "should not appear" not in job.description, "script bodies are not text"
    assert "color:red" not in job.description, "style bodies are not text"


@respx.mock
async def test_a_real_json_ld_description_is_not_replaced_by_the_body() -> None:
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(200, text=sitemap((JOB1, "x")))
    )
    respx.get(JOB1).mock(return_value=httpx.Response(200, text=JSONLD_PAGE))
    desc = (await fetch(entry()))[0].description
    assert "JOB PURPOSE" in desc
    assert "page" not in desc.casefold().split(), "the <body> text must not leak in"

"""Slug resolution.

The dataset is third-party and can restructure at any refresh, so the reader is
tested against every plausible shape rather than one assumed schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobscan.cli import app
from jobscan.slugs import SlugIndex, normalise

runner = CliRunner()


def _write(d: Path, name: str, payload: object) -> None:
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "ats-data"
    d.mkdir()
    # shape 1: bare list of slugs
    _write(d, "greenhouse_companies.json", ["octoenergy", "gresearch", "stripe"])
    # shape 2: list of objects
    _write(
        d,
        "lever_companies.json",
        [
            {"slug": "vitol", "name": "Vitol"},
            {"identifier": "axpo", "company": "Axpo Group"},
        ],
    )
    # shape 3: mapping
    _write(d, "ashby_companies.json", {"modoenergy": "Modo Energy"})
    # shape 4: wrapper around a list
    _write(
        d,
        "workday_companies.json",
        {"companies": [{"tenant": "centrica", "display_name": "Centrica plc"}]},
    )
    return d


def test_loads_every_shape(data: Path) -> None:
    index = SlugIndex.load(data)
    assert index.kinds == {"greenhouse": 3, "lever": 2, "ashby": 1, "workday": 1}
    assert len(index) == 7


def test_unrecognised_filename_is_skipped(data: Path, tmp_path: Path) -> None:
    _write(data, "random_export.json", ["whatever"])
    assert len(SlugIndex.load(data)) == 7, "unknown ATS files must not be guessed at"


def test_malformed_file_does_not_break_the_load(data: Path) -> None:
    (data / "workable_companies.json").write_text("{not json", encoding="utf-8")
    assert len(SlugIndex.load(data)) == 7


def test_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    index = SlugIndex.load(tmp_path / "nope")
    assert not index
    assert len(index) == 0


def test_exact_slug_scores_top(data: Path) -> None:
    hits = SlugIndex.load(data).search("octoenergy")
    assert hits[0].slug == "octoenergy"
    assert hits[0].score == 1.0


def test_finds_the_slug_from_the_trading_name(data: Path) -> None:
    """The real use: you know 'Octopus Energy', not 'octoenergy'."""
    hits = SlugIndex.load(data).search("Octopus Energy")
    assert hits, "should find something for a plausible company name"
    assert hits[0].slug == "octoenergy"


def test_matches_a_legal_name_against_a_plain_one(data: Path) -> None:
    hits = SlugIndex.load(data).search("Centrica")
    assert hits[0].slug == "centrica"
    assert hits[0].kind == "workday"


def test_kind_filter(data: Path) -> None:
    hits = SlugIndex.load(data).search("vitol", kind="greenhouse")
    assert hits == []
    assert SlugIndex.load(data).search("vitol", kind="lever")[0].slug == "vitol"


def test_nonsense_query_returns_nothing(data: Path) -> None:
    assert SlugIndex.load(data).search("zzzqqqxyzzy") == []


def test_config_line_is_paste_ready(data: Path) -> None:
    hit = SlugIndex.load(data).search("Vitol")[0]
    assert hit.config_line == "  - {kind: lever, slug: vitol, label: Vitol}"


def test_workday_config_line_flags_the_unknowable_site(data: Path) -> None:
    """No directory can supply a Workday site id, so say so rather than guess."""
    hit = SlugIndex.load(data).search("Centrica")[0]
    assert "site: FIXME" in hit.config_line
    assert "host: wd3" in hit.config_line


def test_normalise_strips_corporate_noise() -> None:
    assert normalise("Vitol Group Ltd.") == normalise("vitol")
    assert normalise("ACWA Power") != normalise("masdar")


def test_duplicate_slugs_collapse_keeping_a_name(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "a_greenhouse_companies.json", ["acme"])
    _write(d, "b_greenhouse_companies.json", [{"slug": "acme", "name": "Acme Corp"}])
    index = SlugIndex.load(d)
    assert len(index) == 1
    assert index.search("acme")[0].name == "Acme Corp"


# --- CLI ------------------------------------------------------------------


def test_slugs_command_prints_paste_ready_lines(data: Path) -> None:
    result = runner.invoke(app, ["slugs", "Octopus Energy", "--data", str(data)])
    assert result.exit_code == 0, result.output
    assert "octoenergy" in result.output
    assert "kind: greenhouse" in result.output


def test_slugs_command_without_a_dataset_explains_where_to_get_one(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["slugs", "Vitol", "--data", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "job-board-aggregator" in result.output


def test_slugs_command_accepts_several_companies(data: Path) -> None:
    result = runner.invoke(app, ["slugs", "Vitol", "Modo Energy", "--data", str(data)])
    assert result.exit_code == 0
    assert "vitol" in result.output
    assert "modoenergy" in result.output


# --- regressions: noise floor and the Workday triple ------------------------


def test_entity_suffix_slug_does_not_match_every_query(tmp_path: Path) -> None:
    """`sa` and `bv` normalise to the empty string, and '' is a substring of
    everything, so they used to score 0.85 against all 18 companies at once."""
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "greenhouse_companies.json", ["sa", "bv", "co", "octoenergy"])
    for query in ("Masdar", "ADNOC", "Statkraft", "Octopus Energy"):
        for hit in SlugIndex.load(d).search(query):
            assert hit.slug not in {"sa", "bv", "co"}, (
                f"{hit.slug!r} scored {hit.score} against {query!r}"
            )


def test_short_slug_inside_a_query_is_not_a_match(tmp_path: Path) -> None:
    """`slug in query` forced any short token to 0.85: 'as' in 'masdar'."""
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "greenhouse_companies.json", ["as", "ee", "raft", "ona", "fi"])
    for query in ("Masdar", "Hartree", "Statkraft", "National Grid", "Trafigura"):
        for hit in SlugIndex.load(d).search(query):
            assert hit.score < 0.85, (
                f"{hit.slug!r} forced to {hit.score} against {query!r}"
            )


def test_query_inside_a_longer_slug_still_matches(tmp_path: Path) -> None:
    """The half of the containment rule that earns its keep must survive."""
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "greenhouse_companies.json", ["octoenergy"])
    assert SlugIndex.load(d).search("octo")[0].slug == "octoenergy"


def test_workday_pipe_triple_splits_into_tenant_host_site(tmp_path: Path) -> None:
    """The recommended dataset ships Workday as `tenant|host|site`."""
    d = tmp_path / "d"
    d.mkdir()
    _write(
        d,
        "workday_companies.json",
        ["centrica|wd3|centrica", "trafigura|wd3|trafiguracareersite"],
    )
    hit = SlugIndex.load(d).search("Centrica")[0]
    assert hit.slug == "centrica"
    assert hit.site == "centrica"
    assert hit.host == "wd3"


def test_workday_pipe_config_line_carries_the_real_site(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "workday_companies.json", ["gunvor|wd3|gunvor_careers"])
    line = SlugIndex.load(d).search("Gunvor")[0].config_line
    assert "slug: gunvor" in line
    assert "site: gunvor_careers" in line
    assert "host: wd3" in line
    assert "FIXME" not in line


# --- regression: short strings match too well on ratio alone ----------------


def test_short_fuzzy_match_does_not_clear_the_threshold(tmp_path: Path) -> None:
    """`vitl` is a vitamins brand and `aqa` is an exam board. A one-character
    difference in a four-character token is proportionally huge, so a high
    SequenceMatcher ratio over short strings carries almost no information."""
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "ashby_companies.json", ["vitl"])
    _write(d, "workday_companies.json", ["aqa|wd3|aqa"])
    for query in ("Vitol", "TAQA"):
        for hit in SlugIndex.load(d).search(query):
            assert hit.score < 0.85, f"{hit.slug!r} scored {hit.score} for {query!r}"


def test_long_fuzzy_match_still_clears_the_threshold(tmp_path: Path) -> None:
    """The guard must not cost us the one real fuzzy win in the whole set."""
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "lever_companies.json", ["octoenergy"])
    hits = SlugIndex.load(d).search("Octopus Energy")
    assert hits and hits[0].slug == "octoenergy"
    assert hits[0].score >= 0.85


def test_short_slug_still_wins_on_an_exact_match(tmp_path: Path) -> None:
    """Capping the fuzzy path must not touch exact or containment matches."""
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "ashby_companies.json", ["vitl", "octoenergy"])
    index = SlugIndex.load(d)
    assert index.search("vitl")[0].score == 1.0
    assert index.search("octo")[0].score >= 0.85

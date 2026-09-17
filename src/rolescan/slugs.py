"""Slug resolution against a harvested ATS company directory.

The hard part of this tool is not fetching, it is knowing that Octopus Energy's
Greenhouse board is `octoenergy` rather than `octopus` or `octopusenergy`.
Guessing gives roughly the hit rate you would expect from guessing.

Public datasets exist that have already harvested this. The largest is
Feashliaa/job-board-aggregator, which publishes `data/*_companies.json`, one
file per ATS, holding on the order of 95,000 company identifiers, refreshed
daily. It is CC BY-NC 4.0, so personal use is fine and commercial use is not.

Download those files anywhere, point `rolescan slugs` at the directory, and
search it. The reader is deliberately format-tolerant: it accepts a bare list
of slugs, a list of objects, or a mapping, because a third-party dataset can
restructure at any time and a rigid parser would break on the next refresh.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

__all__ = ["Candidate", "SlugIndex", "normalise"]

log = logging.getLogger(__name__)

#: Filename stem fragment -> rolescan source kind.
KIND_HINTS: dict[str, str] = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "ashby": "ashby",
    "workday": "workday",
    "smartrecruiter": "smartrecruiters",
    "workable": "workable",
    "recruitee": "recruitee",
}

_SLUG_KEYS = (
    "slug",
    "identifier",
    "token",
    "board",
    "board_token",
    "company_slug",
    "id",
    "key",
    "tenant",
)
_NAME_KEYS = ("name", "company", "company_name", "display_name", "title", "label")
# Legal-entity suffixes only. Do NOT add domain words here: stripping "energy"
# turns "Octopus Energy" into "octopus", which no longer resembles the real
# slug `octoenergy` and drops the match below threshold. In an energy job
# search those words carry most of the signal.
_NOISE = re.compile(
    r"\b(the|inc|ltd|limited|llc|plc|group|holdings|co|corp|"
    r"corporation|company|gmbh|ag|sa|nv|bv|pjsc|llp)\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Shortest query that may win on containment alone. Below this a token is a
#: substring of too much of the index to mean anything.
_MIN_CONTAINMENT = 4

#: Shortest pair of strings a SequenceMatcher ratio may be trusted over. One
#: character of difference in a four-character token is 25% of the word, so a
#: high ratio there is noise: "vitl"/"vitol" scores 0.89 and "aqa"/"taqa" 0.86,
#: both higher than the genuine "octoenergy"/"octopusenergy" at 0.87.
_MIN_FUZZY = 6

#: What a ratio over short strings is capped to: visible in the table, below
#: the threshold at which a hit is worth acting on.
_SHORT_CAP = 0.84


def normalise(text: str) -> str:
    """Fold a company name or slug to a comparable key.

    "Octopus Energy Ltd." and "octoenergy" will not collapse to the same thing,
    and should not: that is what fuzzy scoring is for. This only removes the
    noise that reliably differs between a legal name and a board token.
    """
    lowered = text.casefold().strip()
    lowered = _NON_ALNUM.sub(" ", lowered)
    return _NON_ALNUM.sub("", _NOISE.sub(" ", lowered))


def _ratio(a: str, b: str) -> float:
    """Similarity, distrusted when either side is too short to be meaningful.

    Length ratio is NOT the discriminator here, despite being the obvious one:
    "octoenergy" against "octopusenergy" is 0.77 balanced, LESS than the wrong
    "vitl"/"vitol" at 0.80, and "taxo"/"axpo" is perfectly balanced at 1.00
    while being a different company. Absolute length is what separates them.
    """
    score = SequenceMatcher(None, a, b).ratio()
    if min(len(a), len(b)) < _MIN_FUZZY:
        return min(score, _SHORT_CAP)
    return score


@dataclass(slots=True, frozen=True)
class Candidate:
    kind: str
    slug: str
    name: str
    score: float
    site: str = ""
    host: str = ""

    @property
    def config_line(self) -> str:
        """A line you can paste straight into config.yaml's `sources`."""
        label = self.name or self.slug
        if self.kind == "workday":
            # A directory that ships only a tenant cannot supply the site, so
            # say FIXME rather than guess; one that ships the triple can.
            return (
                f"  - {{kind: workday, slug: {self.slug}, "
                f"site: {self.site or 'FIXME'}, host: {self.host or 'wd3'}, "
                f"label: {label}}}"
            )
        return f"  - {{kind: {self.kind}, slug: {self.slug}, label: {label}}}"


class SlugIndex:
    """An in-memory index of (kind, slug, name) triples."""

    def __init__(self, entries: list[tuple[str, str, str, dict[str, str]]]) -> None:
        self.entries = entries

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    @property
    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for kind, _, _, _ in self.entries:
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, directory: Path) -> SlugIndex:
        if not directory.is_dir():
            return cls([])
        entries: list[tuple[str, str, str, dict[str, str]]] = []
        for path in sorted(directory.rglob("*.json")):
            kind = cls._kind_for(path)
            if kind is None:
                log.debug("skipping %s: cannot tell which ATS it is", path.name)
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("could not read %s: %s", path, e)
                continue
            entries.extend(
                (kind, slug, name, opts) for slug, name, opts in cls._extract(payload)
            )
        # Same slug can appear in several files; keep the first, richest name.
        # Workday keys on the site too, because one tenant can host several
        # branded career sites and they are not interchangeable.
        seen: dict[tuple[str, str, str], tuple[str, dict[str, str]]] = {}
        for kind, slug, name, opts in entries:
            key = (kind, slug, opts.get("site", ""))
            if key not in seen or (not seen[key][0] and name):
                seen[key] = (name, opts)
        return cls([(k, s, n, o) for (k, s, _), (n, o) in seen.items()])

    @staticmethod
    def _kind_for(path: Path) -> str | None:
        stem = path.stem.casefold()
        for fragment, kind in KIND_HINTS.items():
            if fragment in stem:
                return kind
        return None

    @staticmethod
    def _split_triple(item: str) -> tuple[str, str, dict[str, str]]:
        """`tenant|host|site`, the shape the recommended Workday dump uses.

        Treating the whole string as the slug produces a slug that resolves to
        nothing and a `site: FIXME` beside the site value it was handed.
        """
        if item.count("|") == 2:
            tenant, host, site = (part.strip() for part in item.split("|"))
            if tenant and host and site:
                return tenant, "", {"host": host, "site": site}
        return item, "", {}

    @classmethod
    def _extract(cls, payload: Any) -> list[tuple[str, str, dict[str, str]]]:
        """Pull (slug, name, options) triples out of whatever shape is used."""
        if isinstance(payload, dict):
            # Either a wrapper around the real list, or a slug -> name mapping.
            for key in ("companies", "data", "results", "items", "boards"):
                if isinstance(payload.get(key), list | dict):
                    return cls._extract(payload[key])
            out: list[tuple[str, str, dict[str, str]]] = []
            for slug, value in payload.items():
                if isinstance(value, str):
                    out.append((str(slug), value, {}))
                elif isinstance(value, dict):
                    out.append((str(slug), cls._first(value, _NAME_KEYS), {}))
                else:
                    out.append((str(slug), "", {}))
            return out

        if not isinstance(payload, list):
            return []

        out = []
        for item in payload:
            if isinstance(item, str):
                out.append(cls._split_triple(item))
            elif isinstance(item, dict):
                slug = cls._first(item, _SLUG_KEYS)
                if slug:
                    opts = {
                        k: str(item[k])
                        for k in ("site", "host")
                        if isinstance(item.get(k), str) and item[k].strip()
                    }
                    out.append((slug, cls._first(item, _NAME_KEYS), opts))
        return out

    @staticmethod
    def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    # -- searching ----------------------------------------------------------

    def search(
        self, query: str, *, limit: int = 8, kind: str | None = None
    ) -> list[Candidate]:
        want = normalise(query)
        raw = query.casefold().strip()
        scored: list[Candidate] = []

        for entry_kind, slug, name, opts in self.entries:
            if kind and entry_kind != kind:
                continue
            score = self._score(raw, want, slug, name)
            if score >= 0.55:
                scored.append(
                    Candidate(
                        entry_kind,
                        slug,
                        name,
                        round(score, 3),
                        opts.get("site", ""),
                        opts.get("host", ""),
                    )
                )

        scored.sort(key=lambda c: (-c.score, c.kind, c.slug))
        return scored[:limit]

    @staticmethod
    def _score(raw: str, want: str, slug: str, name: str) -> float:
        slug_n, name_n = normalise(slug), normalise(name)
        if raw == slug.casefold():
            return 1.0
        if name and raw == name.casefold():
            return 0.99
        if want and want in (slug_n, name_n):
            return 0.97
        best = _ratio(want, slug_n)
        if name_n:
            best = max(best, _ratio(want, name_n))
        # A containment match beats a middling edit-distance score: "octo" is a
        # strong signal inside "octoenergy" but scores poorly on ratio alone.
        #
        # Only the query-inside-the-candidate direction earns this. The reverse
        # floored any short token at 0.85 ("as" inside "masdar"), and slugs that
        # are pure entity suffixes normalise to "", which is inside everything.
        if len(want) >= _MIN_CONTAINMENT:
            if slug_n and want in slug_n:
                best = max(best, 0.85)
            if name_n and want in name_n:
                best = max(best, 0.88)
        return best

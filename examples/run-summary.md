# Sample run summary

The counters and footer from one real `jobscan scan` run, 25 August 2026,
against a private `config.yaml` whose source list is the one reproduced in
`energy-trading.yaml`.

**The ranked postings have been removed.** A digest is a record of one
person's job search — which employers they are looking at, which roles they
were told they are structurally excluded from — and that is not something to
publish. What is left is the part that describes the tool rather than the
user: the run counters and the per-source outcome reporting.

Reproducing the numbers below needs your own config, your own source list and
a network connection, so treat them as one measurement rather than a
benchmark.

---

```
# Job scan, Tuesday 25 August 2026

**1 worth a look**, 3 blocked. Scanned 526 unique postings from 9 sources.
0 already seen. 490 filtered before scoring. 36 scored, 0 from cache.

[... 4 ranked postings, removed ...]

---

**Sources that failed this run**

- `greenhouse/gresearch` FetchError: https://boards-api.greenhouse.io/v1/boards/gresearch/jobs: HTTP 404
- `lever/vitol` FetchError: https://api.lever.co/v0/postings/vitol: HTTP 404
- `lever/axpo` FetchError: https://api.lever.co/v0/postings/axpo: HTTP 404

Run `jobscan discover` to check the slugs.

**Sources skipped (not searched)**

- `adzuna/gb` no credentials (set ADZUNA_APP_ID and ADZUNA_APP_KEY)
```

---

## What the counters say

- **526 unique postings from 9 sources.** Deduplication happens across
  boards, keeping the richest copy, so a role listed on both an employer's
  Workday tenant and an aggregator is counted once.
- **490 filtered before scoring, 36 scored.** The deterministic keyword stage
  discarded 93% of the run before any LLM call. On this run every survivor was
  scored on keywords alone — `keyword only` — because no working LLM backend
  was configured at the time.
- **0 from cache** because this was the first run against these sources.
  Verdicts are keyed on a content hash of the posting text, so a second run
  over unchanged postings scores nothing.
- The three 404s were unverified slug guesses, and they are exactly what
  `jobscan discover` exists to catch before a scan. All three employers were
  later resolved onto the right platform — G-Research and Vitol are not on
  Greenhouse or Lever at all, and Axpo publishes schema.org markup through
  Teamtailor. `energy-trading.yaml` records the corrected entries.

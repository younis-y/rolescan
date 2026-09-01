Put harvested ATS company directories here as JSON, then:

    jobscan slugs "Octopus Energy" --data ats-data

Filenames must name their ATS so the loader knows what it is reading, e.g.
`greenhouse_companies.json`, `lever_companies.json`, `ashby_companies.json`,
`workday_companies.json`. Files it cannot classify are skipped rather than
guessed at.

Suggested source: https://github.com/Feashliaa/job-board-aggregator (data/),
CC BY-NC 4.0. Personal job searching is fine; commercial use needs permission.

Any of these shapes parse:
    ["octoenergy", "gresearch"]
    [{"slug": "vitol", "name": "Vitol"}]
    {"modoenergy": "Modo Energy"}
    {"companies": [{"tenant": "centrica", "display_name": "Centrica plc"}]}

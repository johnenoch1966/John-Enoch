# data/

Persistent state ("long-term memory") of the research agent. Committed so that
every run, whether local, CI or Cloud Run, builds on what came before.

| File | Purpose |
|---|---|
| `evidence.db` | SQLite store of sources, factor claims and root-cause mappings (de-duplicated across runs). |
| `use_cases.json` | Cached top-50 enterprise agent use-case catalog (rebuild with `--refresh-scope`). |
| `report.json` | Machine-readable latest report; used to diff the next run against. |
| `history.jsonl` | One line per run: evidence growth over time. |

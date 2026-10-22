# research-data branch

Generated output of the research agent, kept **separate from the code on
`main`** so the code branch stays small and reviewable.

| Path | Contents |
|---|---|
| `data/evidence.db` | Long-term memory: sources, claims, root-cause and industry tags (SQLite, PII-masked) |
| `data/use_cases.json` | Cached top-50 use-case catalog |
| `data/report.json`, `data/history/` | Structured report for the latest run and each previous run |
| `reports/index.html`, `reports/history/` | Rendered HTML reports (published to GitHub Pages) |

How it's updated:
- **Locally:** run the agent, approve publication, then `make publish-data`.
- **Automatically:** the `Refresh report` workflow on `main` runs the agent and opens a pull request against this branch. Merging that PR (human approval) triggers the Pages deployment.

Live report: https://johnenoch1966.github.io/John-Enoch/

# Agent evaluation suite

Automated, repeatable measurement of agent quality against **golden datasets**,
used as a **regression gate in CI/CD**.

## Golden datasets (`golden/`)

| File | Cases | Labels |
|---|---|---|
| `root_cause.jsonl` | 32 hand-labelled adoption factors across all 7 industries and both segments (copilot and workflow), with a mix of vendor and independent sources. Includes the canonical *"Access to connectors not enabled → Risk & Auditability"* semantic-bias case. | acceptable root-cause ids and expected industries |
| `pii.jsonl` | 10 PII cases (9 types) and 10 **false-positive traps**: survey statistics, years, prices, version numbers, regulation ids | expected PII types and facts that must survive masking |

Frozen inputs for the analyst evals live in `fixtures/`: an evidence digest, an industry digest, the catalog, and the last published brief. Regenerate them deliberately with `make eval-fixtures`, then re-baseline.

## Suites

| Suite | Kind | What runs | Metrics |
|---|---|---|---|
| `root_cause_agent` | **live LLM** | the production `root_cause_mapping_loop` (gate + semantic-bias agent and its real tools) maps the golden claims in an isolated temporary store | top-1 / any accuracy, industry match, coverage |
| `segment_analyst` | **live LLM** | the production segment analyst on the frozen digest | schema validity, segment and use-case coverage, section completeness, **citation validity**, items cited, conciseness |
| `industry_analyst` | **live LLM** | the production industry analyst on the frozen industry digest | schema validity, 5/5/5 completeness for all 7 industries, citation validity, conciseness |
| `pii_masking` | deterministic | the masking rules on the golden PII set | recall, false-positive rate, keep rate |
| `taxonomy_lookup` | deterministic | the root-cause hint tool | top-1 and top-3 hit rate |
| `synthesis_rubric` | deterministic | the rubric on the last published brief | as for the analysts |

## Gating

- `thresholds.json`: minimum acceptable score per metric (for false-positive rates, a maximum).
- `baseline.json`: last accepted scores. A drop greater than the tolerance (default 0.05) fails the run, even if the score is still above the threshold.
- Results go to `results/latest.json` and the GitHub job summary.

| Where | When | Suites |
|---|---|---|
| `ci.yml` → `evals` job | every push and PR | offline (blocking; the Docker job depends on it) |
| `evals.yml` | PRs touching prompts, agents, schemas or taxonomy; nightly (model drift); manual | offline + **live** |
| `.cloudbuild/pr_checks.yaml` | PRs | offline |
| `.cloudbuild/staging.yaml` | before the image is built | **live** (blocks deployment) |

```bash
make eval            # offline suites + gates
make eval-live       # live suites (needs: gcloud auth application-default login)
make eval-baseline   # accept current scores as the new baseline
```

## Findings log

| Date | Suite | Finding | Action | Result |
|---|---|---|---|---|
| 2026-11-02 | `segment_analyst` (live) | **Regression caught:** `use_case_coverage` = **0.65** (threshold 0.90). The analyst silently skipped about a third of the catalog use cases when ranking by impact, a non-deterministic failure that the previous production run had not shown. | The prompt now passes the catalog grouped by segment, with explicit per-segment counts as a coverage contract ("exactly N entries; never skip"). | 1.00 and 0.99 on two re-runs; baseline recorded |
| 2026-11-02 | `root_cause_agent` (live) | First baseline: 32/32 correct primary root cause and industry, including the semantic-bias case "connectors not enabled → Risk & Auditability". The deterministic lookup tool alone scores 0.75 top-1, so the agent adds measurable value. | - | baseline 1.00 |

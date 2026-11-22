# 0011: Real-world cases from Fivetran's dbt packages

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-21.

The question: does VeriMend fix real breakages, not only ones we injected? This note covers the free part: mining, building and checking. No model was called. The scored runs (baseline ×3 and VeriMend ×3, verifier watching, $3 cap) come later, after `verifier-v2` and pre-registered predictions.

## Chosen

1. **The source is Fivetran's dbt packages** (`github.com/fivetran/dbt_*`, Apache-2.0). 62 of 89 active packages record DuckDB support in their CHANGELOG, added in August 2026. Each ships an `integration_tests` project with seed CSVs. Each release section of the CHANGELOG names its pull requests, so the parent commit of a fix PR is a real broken state and the PR is a human's reference fix.
2. **Two kinds of candidate** (`python -m bench.realworld.mine`):
   - `bug_fix`: a PR of a release after DuckDB support whose notes read like a bug fix;
   - `duckdb_port`: the PR that added DuckDB support. Its parent is shipped code that did not run on DuckDB until a human adapted it.
3. **The harness comes from the fix commit; the package code from the parent.** A fix PR often adds the seed rows that show its bug, so a case is "the package as shipped at the parent, run on data that triggers the bug". The scenario vars come from the fix commit's `ci/test_scenarios.yml`: the first scenario in which the parent fails and the fix passes.
4. **A case is one self-contained dbt project, offline** (`bench/realworld/lib.py`):
   - The package becomes the root project. The integration project's profile, vars, dispatch and flags are merged into its `dbt_project.yml`, and its singular tests move to `tests/integration_tests/`. Upstream installs the package as `local: ../`, which needs a project folder that reaches outside itself, and every tool we have takes one folder.
   - The packages it installs from dbt Hub are vendored under `vendor/` as `local:` packages, with their own package files removed, so `dbt deps` (which the verifier runs) never needs the network.
   - The seeds are loaded into DuckDB by `dbt seed` with the integration project's own seed config, and read as sources. The agent can read the data but not edit it, as in decision 0005.
   - One git commit with a neutral message, fixed identity and date. `docs/`, CI, release tooling and binary files are left out, and any CHANGELOG section that points at the fix is removed. `check` fails a case if any file or the git log names the PR, its URL or the fix commit.
   - The network is needed once per case, to fill a cache under `~/.verimend/realworld/cache/<id>/`; every build after that is offline.
5. **The grader is the suite's own** (`bench/realworld/grade.py`), sharing no code with the verifier. A run passes when the build passes, no test was removed or changed (contracts count), the vendored packages are untouched, and every model of the answer key is there and equal row for row.
   - **The answer key is the whole fix commit** built on the same data, not the reference limited to the case's files, so "the reference passes" is not circular.
   - It is built twice; a model that differs between the two builds is left out and reported as unstable.
   - **New rule, `vendor_unchanged`**: patching `fivetran_utils` is not a fix to the package.
6. **`python -m bench.realworld check`** keeps a case only if: two builds are identical; nothing leaks the fix; the parent fails on exactly the nodes `case.json` lists; the fix commit builds clean; doing nothing fails; the reference passes.

7. **Grading builds run DuckDB on one thread** (dbt-duckdb `settings: {threads: 1}`). Unordered aggregates (`string_agg`) otherwise come out in a different order on each build. In Shopify, two key builds agreed and the reference then differed on a model nobody had touched. The agent's own profile keeps DuckDB's default.
8. **A model downstream of an unstable one is unstable too**, by the manifest's `child_map`. Two builds agreeing on it can be luck.

## What the check showed

- **Mining** ([candidates.json](../bench/results/realworld-mine-2026-11-21/candidates.json), [mine.txt](../bench/results/realworld-mine-2026-11-21/mine.txt)): 89 active packages, 62 with DuckDB support, 123 candidate PRs (61 bug fixes, 62 DuckDB ports).
- **Screen** ([screen.json](../bench/results/realworld-mine-2026-11-21/screen.json)): 78 merged PRs that change package SQL or YAML and are not fixes for another warehouse only (48 bug fixes, 30 ports). Each one runs every CI scenario at the parent and at the fix, on the fix's harness, in the upstream layout. 19 qualify, meaning the parent fails and the fix passes in some scenario: 6 bug fixes and 13 ports.
- **Check** ([check.json](../bench/results/realworld-check-2026-11-21/check.json)): **17 of 17 cases ok.** Each builds the same twice, leaks nothing, fails at the parent on exactly the listed nodes, builds clean at the fix, fails when nothing is done, and passes with its reference.

| Case | Kind | Merged | Fails at the parent | Reference |
| --- | --- | --- | --- | --- |
| `hubspot-215` | bug fix | 2026-09-22 | `stg_hubspot__conversation_thread`: a contact id overflows `int` | 2 files |
| `quickbooks-225` | bug fix | 2026-10-15 | `unique` on `quickbooks__general_ledger.unique_id`: bundle lines fan out | 1 file |
| `shopify-167` | bug fix | 2026-08-24 | `unique` on `stg_shopify_gql__metafield.unique_key`: the key misses the owner | 2 files |
| `zendesk-290` | bug fix | 2026-11-04 | `int_zendesk__field_history_pivot` does not compile: a custom field named like a standard one | 2 files |
| `amazon_selling_partner-19` | port | 2026-08-18 | 2 marts: string-to-number cast | 1 file |
| `facebook_ads-72` | port | 2026-08-18 | `facebook_ads__url_tags`: no DuckDB unnest | 1 file |
| `fivetran_log-203` | port | 2026-08-18 | 2 models: JSON parse of non-JSON text | 1 file |
| `google_play-36` | port | 2026-08-18 | `stg_google_play__earnings` (earnings enabled): text dates | 2 files |
| `greenhouse-47` | port | 2026-08-18 | `stg_greenhouse__scheduled_interview`: unquoted `end` | 1 file |
| `hubspot-211` | port | 2026-08-18 | 2 models: epoch-ms overflow, `array<string>` cast | 18 files |
| `iterable-83` | port | 2026-08-18 | `int_iterable__list_user_unnest`: Postgres JSON function | 2 files |
| `jira-195` | port | 2026-08-18 | `jira__daily_sprint_issue_history`: `array<string>` cast | 4 files |
| `linkedin-65` | port | 2026-08-18 | 2 staging models: no `to_date` | 1 file |
| `marketo-59` | port | 2026-08-18 | `marketo__calendar_spine`: string plus interval | 6 files |
| `netsuite-208` | port | 2026-08-18 | `stg_netsuite2__account_types`: `left` as a column | 5 files |
| `recurly-49` | port | 2026-08-18 | `int_recurly__transactions_date_spine`: string plus interval | 1 file |
| `stripe-157` | port | 2026-08-18 | 2 models (features disabled): no DuckDB timezone macro | 1 file |

Dropped after qualifying:

- **`quickbooks-227`**: the v1.11.0 release PR, with the same parent as `quickbooks-225` and the same fix inside it. It is a duplicate.
- **`tiktok_ads-48`**: the human fix deletes the `not_null` test on `tiktok_ads__advertiser_report.advertiser_id`, which is the test that fails at the parent. By our grade that is weakening a test (CLAUDE.md rule 3), so the human fix itself would fail, and the case cannot be graded honestly.

Not qualifying: 34 bug fixes pass at the parent in every scenario, even on the fix's own data. Most read as value fixes (nulls, inflated sums) that no test in the package catches. 8 more fail at the parent but also at the fix, in scenarios that break on DuckDB either way. Of the 30 ports, 15 pass at the parent in every scenario and 2 fail at both commits.

Caveats for the scored runs:

- **13 of the 17 are DuckDB ports, merged on one day (2026-08-18).** They are real breakages of shipped code, but all of one kind: SQL written for other warehouses. Only 4 are regressions in the ordinary sense.
- **Unstable models are left out of the answer key**: 9 in `jira-195` (including the failing model, so there the grade relies on the build and its tests), 6 in `shopify-167`, 11 in `zendesk-290`. They look time-dependent (date spines up to today, business-hours models).
- **The check took 1 to 2 minutes a case**; the 17 took 22 minutes. Every case is cached under `~/.verimend/realworld/cache/` and builds offline.

## Rejected

- **Running the upstream layout** (`integration_tests` with `local: ../`): it needs `dbt deps` from dbt Hub on every run, and the verifier copies one project folder.
- **Seeds left as CSV files in the project**: the agent could edit the data, which decision 0005 ruled out.
- **The parent's own harness**: most bug-fix PRs add the seed rows that show the bug, so the parent passes on its own data.
- **Comparing against the reference limited to the case's files**: the reference would pass by construction.

## Decided after the check (with Aditya, 2026-11-22)

**Go, with the two kinds reported apart.** 17 cases pass the bar of 8, but only 4 are bug fixes. Every real-world result is reported as two rows, never pooled: the 4 bug fixes (a regression in shipped code, fixed by a human) and the 13 DuckDB ports (shipped code that never ran on DuckDB until a human adapted it). The pre-registration states its predictions per kind. With bug fixes alone it would have been a no-go, so their row is labelled as below the bar of 8.

## Open, for Aditya

- **History**: a case has one commit, so `inspect_history` finds nothing. The fault suite has a baseline and a fault commit.
- **Runner wiring**: `bench.run --source realworld` is not built. A case's dbt project is the git root (`<dest>/project`), its profile is `<dest>/profiles/profiles.yml` (profile `integration_tests`, target `dev`), and the grader needs `broken/`, `pristine/` and `key/`. `grade_case` returns rules in the fault suite's shape.
- **Contamination**: the fixes merged between 2026-08-18 and 2026-11-04, which lowers the risk that a model has seen them. The packages themselves are public and older.
- **Tag `realworld-v1`** before the first scored run.

## A breach of rule 2, during this phase

On 2026-11-21 the session that built these cases broke CLAUDE.md rule 2. Looking for references to this note's old number, it ran `git grep "0007"` over the whole repository and excluded `bench/results/` but not `bench/heldout/`. The output, about 40 lines, put rows of four held-out ADE-bench tasks' answer seeds (`solution__*.csv`) into that session's transcript. The session reported it itself.

- **Exposed:** the IDs of four held-out tasks, which are no secret (rule 2 lets scripts print task IDs), and some of their answer-seed rows, which are.
- **Not exposed elsewhere:** nothing from those lines went into a commit, a pull request or another session. The main session has not opened them.
- **Why the held-out numbers are unaffected:** the agent has been frozen at `agent-v1` since before this session started. Its prompts, tools and hooks have not changed, and this session worked only on `bench/realworld/`. Row accounting's rules were committed before the breach. The inspector's `pr` policy does not change the `fix` policy the benchmarks use.
- **What changed:** the session was archived and not given further work, and CLAUDE.md rule 2 now says every repository-wide search must exclude `bench/heldout/`.
- **Not done:** a re-split. If Aditya wants a held-out set no Claude session has ever seen, the four tasks named in that transcript would be replaced before the phase 8 runs.

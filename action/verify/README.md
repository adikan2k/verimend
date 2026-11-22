# VeriMend inspector

A GitHub Action that checks every pull request to a dbt project on DuckDB, whoever wrote it: a person, Copilot, Cursor, Claude Code or VeriMend itself. It rebuilds your project from the pull request's base on a fresh copy of the database, applies the change, and stamps the result on the pull request.

It costs nothing to run: no API key, no model call. It is the verifier VeriMend's own fixes have to pass, run on its own.

**Try it:** `uv run python scripts/make_demo_repo.py template --dest <folder>` builds a small dbt repository with this Action and three branches to open as pull requests: a weakened test, a NULL month and a good change. Push it to a repository of your own and the inspector stamps each one. [Their stamps on GitHub](../../bench/results/template-demo-2026-11-22/README.md).

## What it checks

| Check | Fails when |
| --- | --- |
| Tests and files | A test is removed, disabled, loosened (severity, `where`, `error_if`), re-tagged, or a test macro is changed. Adding tests is fine. |
| Hidden failures | A model that failed is removed or disabled, or a change adds `limit 0` or an always-false filter. Removing a model with no failure behind it is fine, and listed. |
| Clean rebuild | `dbt build` fails anywhere, on a fresh copy of the base's database. |
| Collateral damage | A model outside the change's lineage has different rows or contents afterwards. Models that differ between two builds of the unchanged base are compared by row count only, or not at all. |
| Row accounting | A LEFT join groups rows whose key found no partner under a NULL key. Summed rows a LEFT join drops because its spine has no key for them are listed, not failed: in our replay a benchmark's own reference fix drops such rows on purpose ([decision 0008](../../docs/decisions/0008-row-accounting.md)). |

Files outside the dbt project (README, CI, scripts) are listed as not checked.

## What it cannot see

It does not know your business rules. A change that builds cleanly, touches no test and computes the wrong number passes. In our runs this was the most common way a confident AI fix was wrong: for example, adding a missing date spine but switching `count(*)` to `count(column)`, which undercounts days with no reviews ([trace](../../bench/results/gate-baseline-ade-2026-11-21/airbnb009/1.jsonl)). A person still reviews the pull request.

## One it caught

Asked to fix a failing build caused by orders with no customer, a plain AI agent found the bad rows, then set both `not_null` tests on `customer_id` to `severity: warn` so the build went green, and reported "fixed". The tests-and-files check named both tests ([trace](../../bench/results/faults-baseline-x3-2026-11-21/bd04/1.jsonl)).

## Setup

```yaml
# .github/workflows/inspector.yml
name: Inspector
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, unlabeled]
permissions:
  contents: read
  pull-requests: write # for the comment; without it the result is in the job summary only
jobs:
  inspect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: adikan2k/verimend/action/verify@verify-v1
        with:
          project-dir: .
          setup-command: python load_raw.py # builds the warehouse; omit if your seeds do
```

Make it a required status check to block merging on a failed check.

| Input | Default | |
| --- | --- | --- |
| `project-dir` | `.` | The dbt project, relative to the repository root |
| `setup-command` | none | Builds the DuckDB warehouse for the base checkout, run from the project folder |
| `database` | the profile's path | The DuckDB file, relative to the project |
| `profiles-dir` | dbt's search | Folder of `profiles.yml`, relative to the project |
| `allow-label` | `verimend:tests-changed` | A pull request with this label may change tests; they are still listed |
| `comment` | `true` | One comment, updated on every push |
| `dbt-version`, `dbt-duckdb-version` | 1.10.23, 1.9.6 | What it installs |

Outputs: `result` (`passed`, `failed`, or `error` when it could not run, which is never a verdict) and `failed-check`.

## Limits

- DuckDB only. The profile's target must be `type: duckdb`.
- Data loaded outside dbt (by `setup-command`) comes from the base, so a pull request that changes that data is checked against the old data. Seeds are part of dbt and are rebuilt.
- Pull requests from forks get the job summary but no comment, because a fork's token cannot write.
- It builds the project twice for its snapshot and once for the change, so it takes about three builds' time.

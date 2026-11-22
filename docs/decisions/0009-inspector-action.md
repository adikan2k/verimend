# 0009: The inspector, for any dbt pull request

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-22.

## Why

The numbers so far say VeriMend is not a more accurate fixer than a plain agent (decision 0007), but that its verifier sees things a reviewer misses: a test set to `warn`, a model disabled, a build that only passes on the author's copy. Those risks are not specific to VeriMend's fixes. Any pull request to a dbt project can carry them, whoever wrote it, and AI-written ones more often than most. So the verifier ships on its own, as a free check on every pull request.

## Chosen (with Aditya)

1. **A GitHub Action only,** `action/verify/`, composite, tagged `verify-v1` after merge. No PyPI package. It installs the `verifier/` package from the Action's own checkout plus pinned dbt-core and dbt-duckdb versions (the ones this repository is tested with), and calls no model, so it needs no API key.
2. **A second policy, `pr`, next to `fix`.** `fix` stays the default and decides every benchmark number, unchanged. `pr` differs where a real team's pull request differs from an agent's fix:

   | Check | `pr` policy |
   | --- | --- |
   | Paths | Files outside the dbt project's resource folders (README, CI, scripts) are listed as not checked, not refused |
   | Tests | Any removed, loosened, re-tagged or severity-lowered test fails, naming each, unless the pull request carries the label. Adding tests, and new test macros, pass |
   | Masking | `limit 0` and always-false filters fail as before. A removed or disabled model fails only if it, or something downstream of it, failed on the base: hiding a failure. A clean removal is listed |
   | Rebuild, collateral, row accounting | As in `fix`. On a green base nothing is excused, so every node must pass |
   | No dbt change | Passes with "nothing to check" |

3. **Fail, allow by label** (with Aditya). A test change fails unless a maintainer adds `verimend:tests-changed`. The changed tests are still listed, so the label is a recorded decision, not a silencer. `--allow-test-changes` is refused without `--policy pr`, so the benchmark cannot be run with it.
4. **The base is where the pull request started.** The default checkout is the merge commit, whose first parent is the base. Otherwise the Action uses the merge-base with the target branch. `setup-command` builds the base's warehouse (for a project whose raw data is loaded by a script), then the verifier snapshots it.
5. **One comment, updated on every push,** found by a hidden marker. Always the job summary too. Forks get the summary only, because their token cannot write, and the Action says so instead of failing.
6. **No input is spliced into a script.** Pull request titles, labels and inputs reach the shell only as environment variables; a test enforces it.
7. **Row accounting fails only on rule B** (decision 0008). Dropped rows are listed in the comment, not failed.
8. **Apache-2.0** for the repository (with Aditya), with a NOTICE for the benchmarks' and the demo's third-party work. The verifier's wheel carries the licence.

## Rejected

- **Publishing the verifier on PyPI.** A second release channel to keep in step, for no user the Action does not already reach.
- **Running the `fix` policy on pull requests.** It refuses any file outside the project and any new test macro, which every real pull request would trip.
- **Allowing test changes by default and only listing them.** A loosened test is the cheapest way to turn a build green, and the one our runs saw most (decision 0007's `bd04`). A failure a maintainer can override with one label costs little.
- **Checking data loaded outside dbt.** The base's `setup-command` builds the data; a pull request that changes the loader is checked against the old data. Stated as a limit in the README.

## Not yet shown

The Action has not run on GitHub yet. Its proof is the public template (phase 6e): three ready pull requests, a weakened test, a NULL-key grouping, and a real fix, with the expected stamp on each. The template's drop-rows example from the 6e plan now passes with a listed loss, because rule A is report-only, so it becomes a NULL-key example for rule B.

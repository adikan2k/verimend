# 0010: A template to try the inspector

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-22.

## Why

The inspector (decision 0009) had only run on this machine. A reader of the README cannot see it work without wiring it into a project of their own. A template with pull requests already open shows real stamps from GitHub Actions, and anyone can build one with a change of their own in a minute.

## Chosen (with Aditya)

1. **A template repository, `adikan2k/verimend-inspector-demo`,** built by `scripts/make_demo_repo.py template`. The jaffle shop is at the root, with its raw data loaded by `scripts/load_raw.py`, and `inspector.yml` runs `verify@verify-v1` on every pull request. No key, no model.
2. **Three pull requests, open on purpose,** one for each outcome:

   | Branch | Change | Stamp |
   | --- | --- | --- |
   | `try/weaken-test` | `not_null` on `customers.customer_id` set to `warn` | Fails at tests |
   | `try/null-month` | Monthly revenue that filters the orders it joins, not the payments: 5 payments of returned orders, 49.00, land in a NULL month | Fails at row accounting (rule B) |
   | `try/good-change` | The same model with an inner join and a test on the month | Passes |

   A test (`tests/test_demo_repo.py`) runs what the Action runs on each branch and checks these three stamps, so the README's promises are tested.
3. **The fixer is opt-in.** `fix.yml` builds on pushes to `main` and calls VeriMend only when the copy has its own `ANTHROPIC_API_KEY` secret: `HAS_KEY: ${{ secrets.ANTHROPIC_API_KEY != '' }}` gates the step. The template has no secret, so it can never spend anyone's key, and the fixer never runs on pull requests.
4. **Kept private** (Aditya, 2026-11-22). It was created private and checked on its three runs, then left private. So the public docs point to the builder and to the recorded runs, not to the repository. Making it public later needs no code change: the README already says "Use this template", and the template flag is set.
5. **The commits use the GitHub noreply address,** set when building, because a fresh repository takes the global git identity.

## Changed from the plan

- **`try/drop-rows` became `try/null-month`.** Rule A is report-only since v2.2 (decision 0008), so a pull request whose LEFT join drops rows passes with a note. The example now shows what still fails: rows grouped under a NULL key. I checked it against the demo's raw data before building, and it is a common bug shape: the filter sits on the joined side instead of the rows being summed.

## Rejected

- **Opening the try pull requests from forks.** A fork's token cannot comment, so visitors would see only a job summary. The branches live in the template itself.
- **A fixer that runs with a key the template provides.** Anyone could trigger spending with a push to their copy's `main`; each copy brings its own key or nothing runs.

## Limits

- Pull requests that people open on the template from their forks get the job summary only. The README says to "Use this template" instead.
- Data loaded by `scripts/load_raw.py` comes from the base, so a pull request that edits `raw_data/` is checked against the old data (decision 0009).

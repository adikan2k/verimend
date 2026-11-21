# VeriMend GitHub Action

Runs after a dbt build fails in CI. VeriMend works out why. When it fixes the failure and its independent verifier passes the fix, it opens a pull request from `verimend/fix-<sha>`. When it escalates or cannot fix it, it posts the diagnosis as a comment instead.

DuckDB projects only, as VeriMend itself.

## Use it

```yaml
name: dbt
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: write        # push the verimend/fix-* branch
  pull-requests: write   # open the pull request, comment on a failing one

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # VeriMend reads the history to find the commit that broke the build
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "dbt-core~=1.10.11" "dbt-duckdb~=1.9.3"
      - run: dbt build
      - if: failure()
        uses: adikan2k/verimend/action@main
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          max-usd: "0.25"
```

| Input | Default | What it does |
| --- | --- | --- |
| `anthropic-api-key` | required | Your Anthropic API key, from a secret |
| `project-dir` | `.` | The dbt project, relative to the repository root |
| `max-usd` | `0.25` | Dollar cap for the run |
| `model` | VeriMend's default | The model to use |
| `max-attempts` | `3` | Fixes the verifier may reject before the run ends as not fixed |
| `github-token` | `github.token` | Token for the branch, pull request and comment |

The output `exit-code` is 0 for a pull request opened, 2 escalated, 3 not fixed, 4 stopped by a cap, and 1 for an error. Only an error fails the step. The run record (`run.json`, `fix.diff` and the text it posted) is kept as the artifact `verimend-run`.

## What you get

**A pull request** into the branch that failed, with five sections: root cause, evidence, diff summary, verifier output and cost. Nobody merges it but you.

**Or a comment** with the verdict, the root cause, the evidence, what the verifier rejected, and the cost. It goes on the failing pull request, or else on the failing commit.

## What it never does

- Push to any branch but its own `verimend/fix-*` branch. It never pushes to your default branch or to the branch that failed.
- Merge anything.
- Run for a pull request from a fork, where it would need your secret. It also never runs for its own fix branches.
- Edit your tests to go green. Its verifier rejects any fix that removes or loosens a test.

## Good to know

- A pull request opened with the default `github.token` does not start other workflows, so CI does not run on the fix branch by itself. Push to the branch, or close and reopen the pull request, to run it.
- With `fetch-depth: 1` the run still works, but VeriMend sees only the last commit, not what it changed.
- History is read only when the dbt project is at the root of the repository.

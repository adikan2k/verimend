# VeriMend

An agent that diagnoses a failed dbt run, proposes a fix on a branch, and lets a separate verifier decide whether the fix worked.

- What to build, phase by phase: `docs/SPEC.md`
- How the replay site must look and move: `docs/DESIGN.md`
- What the phase 1 spikes found (real SDK calls, benchmark setup, measured cost per run): `docs/spikes.md`. Never guess an Agent SDK signature; use what that file records.

The name is "VeriMend" in prose and `verimend` in code, package names and commands.

## Layout

`verimend/` agent, tools, hooks, CLI · `verifier/` the independent verifier, its own package · `bench/` harness, `dev/` tasks, `heldout/` tasks (off-limits), `faults/` our fault suite, `results/` traces and scores · `action/` the GitHub Actions (fix, and `verify/` the inspector) · `site/` Next.js replay site · `demo/` sample dbt project · `scripts/` one-off tools · `docs/` spec, design, spikes, decisions

## Commands

Phase 1 creates these. If one changes, update this list in the same commit.

- All checks (lint, types, tests, Python and site): `uv run python scripts/check.py`
- Python tests: `uv run pytest` · one file: `uv run pytest verifier/tests/test_verifier_checks.py`
- Python lint and types: `uv run ruff check .` and `uv run pyright`
- Build the demo database from `demo/raw_data` (not committed; run before using `demo/jaffle_shop`): `uv run python scripts/build_demo.py`
- Fix a failed dbt project in a sandbox copy (paid; writes `fix.diff` and `run.json` under `~/.verimend/sandboxes/`): `uv run verimend fix --project-dir demo/jaffle_shop --max-usd 0.25`
- Same, then hand the result to GitHub (a pull request from `verimend/fix-<sha>` for a verified fix, else a diagnosis comment; needs `GITHUB_TOKEN`): add `--open-pr`. The GitHub Action in `action/` runs exactly this.
- Demo repository for the Action: `uv run python scripts/make_demo_repo.py build --dest ~/.verimend/demo-repo --action-ref main`, then `break logic` or `break bad-data --repo ~/.verimend/demo-repo` for its broken commits. Nothing in it pushes.
- Verify any diff, no agent involved: `uv run verimend verify --project-dir demo/jaffle_shop --diff fix.diff`
- Inspect a pull request's diff the way the inspector Action does (free, no model): `uv run python -m verifier snapshot --project-dir <base checkout> [--db <its duckdb>] --work <folder>`, then `uv run python -m verifier verify --work <folder> --diff pr.diff --policy pr` (`--allow-test-changes` for the label), and `uv run python -m verifier render --result result.json` for the comment. The Action is `action/verify/` (decision 0009).
- Benchmark, development set only: `uv run python -m bench.run --agent verimend --set dev --limit 5 --max-usd 2`. Agents: `none`, `reference`, `baseline` (paid), `verimend` (paid). Add `--source data-eng-bench` for that benchmark, `--trials 3`, `--tasks a,b` or `--dry-run`. `--with-verifier` has the verifier watch `none`, `reference` or `baseline` (it tells them nothing; VeriMend always has its own). Runs in the Linux runner (Docker Desktop).
- Fault suite (`bench/faults/`, 20 faults, not split): `uv run python -m bench.run --source faults --set all --agent reference --with-verifier --limit 20 --max-usd 0.01`. Agents `none`, `reference`, `baseline` (paid; in its own container, so Docker Desktop must be up) and `verimend` (paid, on this machine).
- Fault suite checks, no model calls (determinism, expected failures, do-nothing fails, reference passes): `uv run python -m bench.faults check` writes `bench/results/faults-check-<date>/check.json`. One fault into a folder to look at: `uv run python -m bench.faults build sd01 --dest ~/.verimend/faults/sd01`.
- Replay recorded fixes through today's verifier, no model calls (free; Docker Desktop for the public sources): `uv run python -m bench.replay --source data-eng-bench --batch replay-<name> --batches <batch>,<batch>` writes `replay.jsonl` and `replay-summary.json`. `--source faults` or `ade-bench`; `--tasks a,b`, `--dry-run`, `--summarise-only`. It runs `bench.run --agent replay` underneath; one recorded diff by hand, against a snapshot folder: `uv run python -m verifier verify --work <snapshot> --diff fix.diff --replay`.
- Real-world cases (`bench/realworld/`, Fivetran dbt packages, no model calls): `uv run python -m bench.realworld.mine` lists candidate PRs; `uv run python -m bench.realworld check` writes `bench/results/realworld-check-<date>/check.json`; `uv run python -m bench.realworld build <case> --dest ~/.verimend/realworld/<case>` builds one (the first build of a case needs the network, later ones are offline); `uv run python -m bench.realworld reference <case>` writes its `reference/fix.diff`.
- Results table from batch summaries: `uv run python scripts/results_table.py --out docs/results-dev.md --batch <name> --batch <name> ...` (`--note "<text>"` adds a line under the table). The fault suite's table: `--out docs/results-faults.md --title "Fault suite"`
- Pre-split task check, no model calls: `uv run python scripts/validate_tasks.py --source ade-bench --limit 30 --max-usd 0.01` (or `--source data-eng-bench`)
- The split, run by Aditya only: `uv run python scripts/split_bench.py --seed 1119 --validation bench/results/validate-2026-11-19/validation.json`
- Site: `pnpm -C site dev`, `pnpm -C site lint`, `pnpm -C site typecheck`, `pnpm -C site test`

Sandboxes, database copies and benchmark checkouts live in `~/.verimend`, never under AppData (see `docs/spikes.md`).

## Environment

Variables are listed in `.env.example`. Never read `.env`. The API key lives only in `.env`: never set `ANTHROPIC_API_KEY` as a system environment variable, because Claude Code would then bill the key instead of the Max plan. The API key, model names and spend caps come from environment variables or `verimend/config.py`, never from code.

## Rules

1. No paid model call runs in a loop without `--limit` and `--max-usd`. The harness prints an estimated cost before it starts and stops when the cap is reached.
2. Never read, list or edit `bench/heldout/`, with any tool, including shell commands. Aditya runs held-out batches himself. The task folders of the benchmark clones in `~/.verimend/vendor/` are off-limits too: the harness reads them inside its containers, scripts may print task IDs and tags but no other content, and Claude Code reads development tasks only from `bench/dev/`. Every search across the repository excludes it, for example `git grep <text> -- . ':(exclude)bench/heldout'` or `rg --glob '!bench/heldout/**'` (decision 0011).
3. Never weaken, skip or delete a verifier check or a hidden check to make a run pass. If a check looks wrong, stop and say so.
4. Every guard ships with a test that proves it blocks what it should.
5. `verifier/` never imports from `verimend/`. The tools in `verimend/tools/` never import the Agent SDK; only `verimend/agent/` does.
6. Model names live only in config: `claude-haiku-5-5` by default, `claude-sonnet-5-5` for the comparison run.
7. Keep every prompt under 100,000 tokens. Cap and summarise tool output before it reaches the model.
8. Every number in the README or on the site comes from a file under `bench/results/` and links to it.
9. The agent opens pull requests. Nothing merges itself and nothing pushes to a default branch.
10. Ask before adding a dependency.
11. The site uses only the tokens in `docs/DESIGN.md`. A new token goes into that file first.

## How we work

- Start each phase in plan mode against `docs/SPEC.md`. No code until the plan is approved.
- Small slices, one commit each, on a feature branch.
- Before calling something done, show evidence: the command you ran and its output, a trace of a real run, or a screenshot at 375, 1280 and 1600 px for site work.
- After each phase, draft `docs/decisions/NNNN-<topic>.md` (what was chosen, what was rejected, why) for Aditya to rewrite in his own words.
- Once the tag `agent-v1` exists, any change to prompts, tools or hooks gets its own decision note, because it changes what the published numbers mean.

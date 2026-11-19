# 0001: Phase 1 setup and spikes

Status: draft by Claude Code for Aditya to rewrite. Date: 2026-11-19.

## Chosen

1. **One uv workspace, with the verifier as its own package.** `verifier/` has its own `pyproject.toml` with no dependencies, and an AST test fails the build if it imports `verimend` or the Agent SDK. This keeps the verifier publishable alone (SPEC §5) and makes rule 5 something a test enforces, not something we promise.
2. **dbt pinned to ADE-bench's image** (`dbt-core~=1.10.11`, `dbt-duckdb~=1.9.3`), so a project that builds on the host builds the same way when the benchmark grades it.
3. **Sandboxes in `~/.verimend`**, outside the repo and outside OneDrive.
4. **ADE-bench's harness runs in a small Linux container** that drives the host's Docker. Grading is ADE-bench's own code, unchanged.
5. **Our agents plug into ADE-bench through its `AgentFactory`.** The pilot did this. The benchmark stages the task and grades it; our code only replaces the agent.
6. **Agent SDK settings:** `tools=[]`, `permission_mode="dontAsk"`, `setting_sources=[]`, a plain-string system prompt, and the key passed through `options.env`. Cost comes from `ResultMessage.model_usage`. A cap stop is caught as `ResultError`.
7. **The price table applies Haiku 5.5's 5x tier per request**, counting cached tokens, because the pricing page defines it that way.

## Rejected

- **`%LOCALAPPDATA%\verimend` for sandboxes.** It was the first choice, but the Claude desktop app is a packaged Windows app and its AppData writes are redirected. Claude Code, a terminal and Docker would each see a different folder.
- **Running ADE-bench natively on Windows.** It builds container paths with `pathlib`, so `/tmp` becomes `\tmp`. Patching vendor code would mean grading with a harness that is not the published one.
- **Installing an Ubuntu WSL distro.** The container runner made it unnecessary.
- **Re-implementing task staging and grading on the host.** It is more code, and it risks grading differently from the benchmark we cite.
- **Summing per-message token usage for cost.** The spike showed those output counts are not final.

## Why it matters for the numbers

- Results are only comparable if grading is the benchmark's own, which is why choices 4 and 5 were made.
- The SDK's dollar cap can overshoot by one request. Spend guards therefore count real spend after each run, and give the SDK a cap below the real one.
- A cwd that changes every run breaks the prompt cache, so cost per run depends on running the agent from a fixed folder.

## Open, for Aditya

- Whether the agent's tool list gains a file-read tool. The SPEC's seven tools cannot read a model before editing it.
- Pin the host's `duckdb` to the image's 1.3.0, or keep every benchmark database write inside the task container.
- Pin the three tasks the pilot used to the development set (listed in `docs/spikes.md`).

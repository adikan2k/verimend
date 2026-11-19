# Phase 1 spikes

What the three phase 1 spikes found on Thu 19 Nov 2026, on Windows 11 with Docker Desktop. Every claim links to the file or command that shows it. Later phases use the SDK calls recorded here and nothing written from memory (CLAUDE.md).

Raw evidence:

- Spike 1 transcripts: [`bench/results/spikes-2026-11-19/`](../bench/results/spikes-2026-11-19/)
- Spike 3 traces: [`bench/results/pilot-2026-11-19/`](../bench/results/pilot-2026-11-19/)
- Spike scripts: [`scripts/spikes/`](../scripts/spikes/)

## Versions

| Piece | Version | Note |
| --- | --- | --- |
| Python | 3.12 (uv) | `.python-version` |
| `claude-agent-sdk` | 0.2.185 | Bundles Claude Code CLI 2.1.328. The Windows wheel needs no Node. |
| `dbt-core` / `dbt-duckdb` | 1.10.23 / 1.9.6 | Pinned `~=1.10.11` / `~=1.9.3` to match ADE-bench's DuckDB image ([`Dockerfile.duckdb-dbt`](https://github.com/dbt-labs/ade-bench/blob/main/docker/base/Dockerfile.duckdb-dbt): dbt-core 1.10.11, dbt-duckdb 1.9.3, duckdb 1.3.0). |
| `duckdb` (host) | 2.0.0 | **Does not match the image's 1.3.0.** See "Open risks". |
| `mcp` client | 2.3.0 | Fields are snake_case: `input_schema`, `is_error`. |
| `dbt-mcp` | latest from `uvx` on 19 Nov | Run with `uvx dbt-mcp`. |
| ADE-bench | `main` on 19 Nov, `pyproject` 1.0.0rc1 | Apache-2.0. Cloned to `~/.verimend/vendor/ade-bench`, never into this repo. |

## Spike 1: Agent SDK, one custom tool and one hook that denies

Script: [`scripts/spikes/sdk_tool_and_hook.py`](../scripts/spikes/sdk_tool_and_hook.py). Nine calls on `claude-haiku-5-5`, about $0.0022 in total. The turn-cap and dollar-cap calls ran twice, because the first try crashed on the uncaught `ResultError`. Phase 1's whole model spend, pilot included, was about $0.017.

### The calls that work

```python
from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    ResultError,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)


@tool("edit_model", "Replace the content of a dbt model file.", {"path": str, "content": str})
async def edit_model(args):  # args is a dict
    return {"content": [{"type": "text", "text": "..."}]}  # add "is_error": True on failure


async def guard(input_data, tool_use_id, context):  # PreToolUse hook
    if allowed(input_data["tool_input"]):
        return {}  # allow
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "why",
        }
    }


server = create_sdk_mcp_server(name="verimend", version="0.0.1", tools=[edit_model])
options = ClaudeAgentOptions(
    model=settings.model_default,
    system_prompt=SYSTEM_PROMPT,  # a plain string replaces the Claude Code prompt
    tools=[],  # removes every built-in tool from the model's view
    mcp_servers={"verimend": server},
    allowed_tools=["mcp__verimend__edit_model"],
    permission_mode="dontAsk",  # anything not pre-approved is denied, never prompted
    setting_sources=[],  # loads no user, project or local Claude settings
    hooks={"PreToolUse": [HookMatcher(matcher="mcp__verimend__edit_model", hooks=[guard])]},
    max_turns=40,
    max_budget_usd=0.25,
    env={"ANTHROPIC_API_KEY": api_key},  # the key goes to the CLI only, never the system env
    cwd=FIXED_DIR,  # see "Caching" below
)
try:
    async for message in query(prompt=prompt, options=options):
        ...  # ResultMessage arrives even when a cap is hit
except ResultError as error:  # raised after that ResultMessage when a cap is hit
    ...  # error.data is the raw result dict
```

### What each call showed

| Question | Answer | Evidence |
| --- | --- | --- |
| Tool name | `mcp__<server key>__<tool name>` | `init.tools` in [run 1](../bench/results/spikes-2026-11-19/sdk_tool_and_hook.query.1.json) |
| Do `tools=[]` + `dontAsk` leave only our tools? | Yes. `init.tools` is `["mcp__verimend__edit_model"]` and the model says it has no shell or file tool. No `ToolSearch` step appeared. | run 1 |
| Does the hook deny with `query()`, or is `ClaudeSDKClient` needed? | `query()` is enough; the README line saying otherwise is out of date. The model gets a tool result with `is_error: true` and the text `PreToolUse:<tool> hook error: <reason>`, and does not retry. The denial is also listed in `ResultMessage.permission_denials`. | run 1 |
| Which credential is used? | `init.apiKeySource` is `ANTHROPIC_API_KEY`: the key passed through `options.env`. No system variable needed. | run 1 |
| Does this repo's CLAUDE.md leak into the agent? | No. With `setting_sources=[]` and `cwd` set to this repo, the model reports no instruction file. The control with `setting_sources=["project"]` quotes `# VeriMend`. | [ctx-none](../bench/results/spikes-2026-11-19/sdk_tool_and_hook.query.ctx-none.json), [ctx-project](../bench/results/spikes-2026-11-19/sdk_tool_and_hook.query.ctx-project.json) |
| What does the CLI add to our system prompt? | An environment block that includes the working directory, even with a plain-string system prompt. | ctx-none: the model quotes the cwd |
| Does our price table match the SDK? | Yes. `pricing.py` on the run's usage gives $0.000434, the same as the SDK's `total_cost_usd`. | run 1 |
| Can cost be summed from `AssistantMessage.usage`? | **No.** Its `output_tokens` is the count when that block streamed (24 and 7 against a final 456). Prompt-side counts are complete, so it is good for prompt size only. | run 1, `per_message_usage` |
| What happens at `max_turns`? | The stream yields a `ResultMessage` with subtype `error_max_turns`, then `query()` raises `ResultError`. `max_turns=1` reported `num_turns: 2`. | [turns1](../bench/results/spikes-2026-11-19/sdk_tool_and_hook.query.turns1.json) |
| What happens at `max_budget_usd`? | Same shape, subtype `error_max_budget_usd`. The cap is checked after each request, so it **overshot by 55%** ($0.000155 against $0.0001). On this stop `ResultMessage.usage` is all zeros, while `model_usage` keeps the real counts. | [budget](../bench/results/spikes-2026-11-19/sdk_tool_and_hook.query.budget.json) |
| Does caching work across runs? | Partly. Within a run, every request after the first reads the cache. Across runs only the stable prefix is reused (718 tokens): the cwd in the environment block was a fresh temp folder each run. | [2-repeat](../bench/results/spikes-2026-11-19/sdk_tool_and_hook.query.2-repeat.json), budget |
| Does Haiku 5.5 think by default? | Yes: 127 to 409 thinking tokens per call, billed as output. | `output_tokens_details` in each transcript |

### Rules this sets for `verimend/agent/`

1. Read cost and tokens from `ResultMessage.model_usage` (or `total_cost_usd`), never from `usage`, and never by summing `AssistantMessage.usage`.
2. Catch `ResultError` after the stream. The run's `ResultMessage` has already arrived; record it, then end the run as "stopped by a cap" (exit code 4).
3. Give the SDK a per-run dollar cap below the real one. The SDK checks after each request, so a run can pass its cap by one request's cost. `BatchBudget` already counts what was really spent.
4. Run the agent from one fixed `cwd` and reach the sandbox through tool arguments, so the environment block, and with it the cache prefix, is the same every run.
5. Always pass `setting_sources=[]` and a plain-string system prompt.

## Spike 2: one public benchmark task graded locally on DuckDB, and the dbt MCP server

### ADE-bench on this machine

| Step | Result |
| --- | --- |
| Harness natively on Windows | **Fails.** ADE-bench builds container paths with `pathlib`, so `/tmp` becomes `\tmp`, and Docker answers 404 ("Could not find the file \tmp"). The harness also crashes printing ✓ to a cp1252 console. |
| Task scripts | Git for Windows converts `setup.sh` and `solution.sh` to CRLF, which breaks them in Linux. The vendor clone is set to `core.autocrlf=false` and `core.eol=lf`. |
| Harness from a Linux container | **Works.** [`scripts/spikes/ade_run.ps1`](../scripts/spikes/ade_run.ps1) runs it in [`ade_runner/Dockerfile`](../scripts/spikes/ade_runner/Dockerfile) against the host's Docker. It mounts the checkout at `/run/desktop/mnt/host/c/...`, the path Docker Desktop uses for host files, so the harness's bind mounts resolve the same for every container. |
| Reference solution, `analytics_engineering002`, `--agent sage` | **PASS**, 3 of 3 dbt tests, 142 s end to end |
| Do-nothing agent, same task, `--agent none` | **FAIL**, 1 of 3 dbt tests, 82 s |

How ADE-bench grades (README and harness source): the project is copied to `/app`, the database to `/app/<name>.duckdb`, then `setup.sh` breaks something. After the agent finishes, the task's `tests/` replace the project's tests. Equality and existence tests are generated from `solution_seeds`. The trial passes only if every test passes. The tests are not in the container while the agent works.

Where the sandboxes live: `~/.verimend`, not `%LOCALAPPDATA%`. The Claude desktop app is a packaged Windows app, so its writes to `AppData` are quietly redirected into its package folder. The same path then means different folders to Claude Code, to a terminal and to Docker. `verimend/config.py` defaults to `~/.verimend` and says why.

### Task counts

ADE-bench (`scripts/spikes/count_ade_tasks.py`, which prints IDs and tags only): 66 task folders, 60 `ready`. The `category` field is empty everywhere. **18 ready tasks are tagged `debugging`**, the fix-style ones. Databases are a GitHub release: 10 files, 213 MB in all.

data-eng-bench ([snowflake-labs/data-eng-bench](https://github.com/snowflake-labs/data-eng-bench)): Apache-2.0, 103 task folders. **19 have `fix` in their name**, which matches SPEC §6; its README files 16 of them under "Development and bug-fixes". DuckDB is the default backend (`retail.duckdb`, about 489 MB via Git LFS), so no Snowflake account is needed. It runs on the Harbor runner with Docker. **Not run today:** getting one oracle run is the first job of phase 2.

### Tasks seen in phase 1, to pin to the development set

The split does not exist yet. The pilot ran on these three tasks, so their prompts reached a model and their results are in this repo. `scripts/split_bench.py` must put them in `bench/dev/` before it splits the rest:

`analytics_engineering002`, `asana001`, `f1007`

No other task's prompt, setup or solution was opened.

### dbt MCP server against local DuckDB

[`scripts/spikes/dbt_mcp_probe.py`](../scripts/spikes/dbt_mcp_probe.py) on `dbt-labs/jaffle_shop_duckdb`, with `DBT_PROJECT_DIR`, `DBT_PATH`, `DISABLE_SEMANTIC_LAYER`, `DISABLE_DISCOVERY`, `DISABLE_ADMIN_API`. Results: [`dbt_mcp_probe.json`](../bench/results/spikes-2026-11-19/dbt_mcp_probe.json).

| Tool | Works | Arguments | Note |
| --- | --- | --- | --- |
| `build` | yes | `node_selection`, `is_full_refresh`, `vars`, `sample`, `yml_selector` | Success returns only `OK`, no per-node results |
| `run` | yes | same as `build` | |
| `test` | yes | `node_selection`, `vars`, `yml_selector` | Success returns only `OK` |
| `compile` | yes | `node_selection`, `yml_selector` | |
| `show` | yes | `sql_query`, `limit` | Returned `{"show": [{"n": 99}]}` |
| `list` | yes | `node_selection`, `resource_type`, `yml_selector` | |
| `parse` | yes | none | |
| `docs` | yes | none | |
| `clone` | yes | `node_selection`, `state_path`, ... | |
| `get_lineage_dev` | yes | `unique_id`, `depth`, `types` | Parents and children as nested JSON |
| `get_node_details_dev` | yes | `node_id` | Takes a selector such as `orders`, not a unique ID |
| `search_product_docs`, `get_product_doc_pages` | not called | | dbt documentation search over the network, not a local tool |

Every call spawns dbt, so each takes about 4 to 5 s. The argument is `node_selection`, not `selector`; the first probe guessed wrong and every call failed validation. `jaffle_shop_duckdb` asks for dbt 1.11 or later, so with our pin the server needs `DBT_VERSION_CHECK=false`. ADE-bench does not offer its MCP plugin set on DuckDB: DuckDB allows one writer at a time, so an MCP server and the agent's own tools cannot hold the same database file at once.

## Spike 3: three-task pilot

[`scripts/spikes/pilot.py`](../scripts/spikes/pilot.py), run with `pilot_run.ps1 -Limit 3 -MaxUsd 0.75`. It uses a throwaway mini-agent with five draft tools (`read_failure`, `read_path`, `query_data`, `edit_file`, `run_dbt`), each output capped at 4,000 characters, and two PreToolUse guards (edit paths, single SELECT). There is no verifier and only one attempt. ADE-bench's own `Harness` staged and graded each task; the agent was plugged in through an `AgentFactory`. Model `claude-haiku-5-5`, caps $0.25 and 40 steps per run. Commit `afe10ef`.

Summary: [`bench/results/pilot-2026-11-19/summary.json`](../bench/results/pilot-2026-11-19/summary.json).

| Task | Graded by ADE-bench | Cost | Steps | Tool calls | Hook blocks | Tokens in / cache read / cache write / out | Largest prompt | Time | Trace |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `analytics_engineering002` | pass, 3 of 3 | $0.0012 | 7 | 6 | 0 | 10 / 8,935 / 3,719 / 1,049 | 3,721 | 13 s | [1.jsonl](../bench/results/pilot-2026-11-19/analytics_engineering002/1.jsonl) |
| `asana001` | pass, 3 of 3 | $0.0105 | 31 | 29 | 1 | 32 / 160,184 / 22,799 / 11,753 | 23,886 | 64 s | [1.jsonl](../bench/results/pilot-2026-11-19/asana001/1.jsonl) |
| `f1007` | pass, 7 of 7 | $0.0030 | 17 | 16 | 0 | 24 / 54,897 / 6,619 / 2,921 | 7,706 | 25 s | [1.jsonl](../bench/results/pilot-2026-11-19/f1007/1.jsonl) |
| **Total** | **3 of 3** | **$0.0146** | | | | | | 271 s with staging and grading | |

**Mean $0.0049 a run, highest $0.0105**, against SPEC §9's estimate of $0.08 to $0.10. The arithmetic checks against `pricing.py`. For `asana001`: 160,184 cache reads at $0.01/M, 22,799 cache writes at $0.125/M and 11,753 output tokens at $0.50/M come to $0.0104, against the SDK's $0.0105. Output tokens, thinking included, are more than half the cost. The largest single prompt was 23,886 tokens, a quarter of the 90,000 limit.

What the runs did:

- `analytics_engineering002`: found the missing comma in `obt_product_inventory.sql` and fixed it.
- `f1007`: found that an inner join on a lookup holding only six status codes dropped about 15,700 of 26,599 rows. It changed the join to a left join and confirmed the row count against the source. `read_failure` found nothing failing here: the bug is wrong data, not a failed run, and the agent worked from the task's prompt.
- `asana001`: a package model coalesces an INTEGER column with a TIMESTAMP. The guard refused an edit inside `dbt_packages/`. The agent then copied the model into the project with a cast, and set the package's copy to `+enabled: false` in `dbt_project.yml`. The hidden tests pass. **This collides with SPEC §5's masking check "no model disabled":** as written, the verifier would reject this correct fix. Rule 3 says not to change a check on our own, so this is flagged for a decision in phase 4. A narrower rule would be "a disabled model must be replaced by an enabled model of the same name".

What the pilot does not show: three tasks and one trial each are not a fix rate. The draft agent saw the task's prompt, had no verifier and only one attempt. The costs say the order of magnitude per run, nothing more.

### Cost estimates that replace SPEC §9's

- **Haiku 5.5:** measured $0.0012 to $0.0105 a run, mean $0.0049. With three attempts, a run that fails twice would cost about three times as much, so plan on **up to $0.03 a run**.
- **Sonnet 5.5:** not measured. It costs 20 times as much per token as Haiku with no long-prompt tier, so by extrapolation expect **about $0.10 a run, up to $0.60 with three attempts**.
- **The full plan for December** (SPEC §6): VeriMend on Haiku at three trials, plus the two ablations on 15 tasks, comes to about 100 runs, or $3 at $0.03 a run. Sonnet on eight tasks is under $5. The plain-agent baseline is **not measured**: it reads files through the SDK's own tools, so its prompts will be larger. Measure it on the development set in phase 2 before trusting the total. Even at ten times VeriMend's cost it stays inside the $33 set aside.

## Open risks

- **DuckDB versions differ.** The host has duckdb 2.0.0 (pulled in by dbt-duckdb 1.9.6); ADE-bench's image has 1.3.0. A database file written by 2.0 may not open in 1.3. Phase 2 should either pin `duckdb` on the host to the image's version, or keep every write to a benchmark database inside the task container, as the pilot does.
- **ADE-bench tasks are prompts, not failed runs.** The harness hands the agent the task's prompt, and some fix-style tasks start from a project that builds cleanly. `read_failure` needs a fallback for "nothing fails, but the prompt says what is wrong".
- **The spec's seven tools have no way to read a file.** The pilot needed `read_path` to see the model it was about to edit; `edit_model` alone would mean editing blind. Phase 3 needs a decision: add a read tool, or have `walk_lineage` return file content.

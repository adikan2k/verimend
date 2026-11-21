# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""Run one batch through ADE-bench's own harness. Inside the Linux runner only.

    python -m bench.ade.inner <batch.json>

bench/run.py writes batch.json, stages the tasks and starts the runner. Here ADE-bench's
`Harness` stages each task in its container, runs the agent and grades the trial with
the task's own tests, unchanged. This module only wraps the agent, to time it, give it
its share of the batch budget and keep the baseline's output; everything after the
harness returns is bench/assemble.py.
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from ade_bench.agents.agent_factory import AgentFactory, NamedAgentFactory
from ade_bench.agents.agent_name import AgentName
from ade_bench.agents.base_agent import AgentResult, BaseAgent
from ade_bench.agents.installed_agents.claude_code.claude_code_agent import ClaudeCodeAgent
from ade_bench.harness import Harness
from ade_bench.harness_models import FailureMode, TerminalCommand

from bench.ade.workspace import AdeWorkspace
from bench.agents import BASELINE_TOOLS, AgentSpec, baseline_cli_flags, get_agent
from bench.assemble import AgentRecord, write_batch
from bench.claude_stream import parse_stream
from bench.grade import grades_from_ade_results
from verimend.agent.run import run_agent_sync
from verimend.config import load_settings
from verimend.hooks.caps import BatchBudget, RunCaps
from verimend.runrecord import AgentRun
from verimend.tools.verify import CONTAINER_SOURCE, CONTAINER_WORK, WorkspaceVerifier


def container_verifier(workspace: AdeWorkspace) -> WorkspaceVerifier:
    """The verifier, uploaded into the task container and run there as its own process."""
    return WorkspaceVerifier(workspace, CONTAINER_WORK, source_dir=CONTAINER_SOURCE)


SETUP_SCRIPT = Path(__file__).with_name("claude-code-setup.sh")
AGENT_LOG = "/tmp/agent_output.log"  # where ADE-bench's installed agents tee their output


class BaselineAgent(ClaudeCodeAgent):
    """ADE-bench's Claude Code agent with a pinned CLI, a turn cap, a dollar cap and no
    web tools. Everything else, including how the prompt is passed, is ADE-bench's."""

    def __init__(self, *, api_key: str, model: str, run_cap_usd: float, max_turns: int) -> None:
        super().__init__(model_name=model, allowed_tools=list(BASELINE_TOOLS))
        self._api_key = api_key
        self._run_cap_usd = run_cap_usd
        self._max_turns = max_turns

    @property
    def _env(self) -> dict[str, str]:
        return {"ANTHROPIC_API_KEY": self._api_key}

    @property
    def _install_agent_script(self) -> Path:
        return SETUP_SCRIPT

    def _run_agent_commands(self, task_prompt: str) -> list[TerminalCommand]:
        extra = baseline_cli_flags(self._run_cap_usd, self._max_turns)
        return [
            TerminalCommand(
                command=c.command + extra,
                min_timeout_sec=c.min_timeout_sec,
                max_timeout_sec=c.max_timeout_sec,
                block=c.block,
                append_enter=c.append_enter,
            )
            for c in super()._run_agent_commands(task_prompt)
        ]


class VerimendAgent(BaseAgent):
    """VeriMend in an ADE-bench task container: our tools act on /app through `docker exec`,
    the model loop runs here in the runner, the verifier runs in the container."""

    NAME = AgentName.NONE  # a label only; staging and grading are ADE-bench's, unchanged

    def __init__(self, *, api_key: str, model: str, caps: RunCaps, output_chars: int,
                 cwd: Path) -> None:  # fmt: skip
        super().__init__()
        self._api_key = api_key
        self._model = model
        self._caps = caps
        self._output_chars = output_chars
        self._cwd = cwd
        self.last_run: AgentRun | None = None

    def perform_task(
        self,
        task_prompt: str,
        session: Any,
        logging_dir: Path | None = None,
        task_name: str | None = None,
    ) -> AgentResult:
        workspace = AdeWorkspace.open(session.container)
        run = run_agent_sync(
            task_prompt,
            workspace,
            model=self._model,
            api_key=self._api_key,
            caps=self._caps,
            cwd=self._cwd,
            output_chars=self._output_chars,
            verifier=container_verifier(workspace),
        )
        self.last_run = run
        return AgentResult(
            input_tokens=0,
            output_tokens=0,
            num_turns=run.num_turns or 0,
            cost_usd=run.cost_usd or 0.0,
            model_name=self._model,
            failure_mode=FailureMode.NONE,
        )


class Recorder:
    """Per-trial records and the batch budget, shared by every agent of the batch."""

    def __init__(self, spec: AgentSpec, budget: BatchBudget, per_run_usd: float) -> None:
        self.spec = spec
        self.budget = budget
        self.per_run_usd = per_run_usd
        self.records: list[AgentRecord] = []

    def next_trial(self, task: str) -> int:
        # Trials of one task run in order (n_concurrent_trials=1), so the count is the index.
        return 1 + sum(1 for r in self.records if r.task == task)


class RecordingAgent(BaseAgent):
    NAME = AgentName.NONE

    def __init__(
        self,
        inner: BaseAgent | None,
        recorder: Recorder,
        run_cap: float | None,
        check_with_verifier: bool = False,
    ):
        super().__init__()
        self._inner = inner
        self._recorder = recorder
        self._run_cap = run_cap
        self._check = check_with_verifier
        if inner is not None:
            self.NAME = inner.NAME

    def perform_task(
        self,
        task_prompt: str,
        session: Any,
        logging_dir: Path | None = None,
        task_name: str | None = None,
    ) -> AgentResult:
        task = (task_name or "unknown").split(".")[0]
        record = AgentRecord(
            task=task,
            trial=self._recorder.next_trial(task),
            started=time.time(),
            run_cap_usd=self._run_cap,
        )
        self._recorder.records.append(record)
        if self._inner is None:
            record.stopped = "batch_cap"
            print(f"{task} trial {record.trial}: not started, batch cap reached", flush=True)
            return AgentResult(input_tokens=0, output_tokens=0, failure_mode=FailureMode.NONE)

        started = time.monotonic()
        verifier = None
        if self._check:  # --with-verifier on a free agent: snapshot before it acts
            verifier = container_verifier(AdeWorkspace.open(session.container))
            record.verifier = {"snapshot": None, "attempts": [], "error": None}
            try:
                record.verifier["snapshot"] = asyncio.run(verifier.prepare())
            except Exception as error:
                record.verifier["error"] = f"snapshot failed: {type(error).__name__}: {error}"
                verifier = None
        try:
            return self._inner.perform_task(
                task_prompt=task_prompt,
                session=session,
                logging_dir=logging_dir,
                task_name=task_name,
            )
        finally:
            if verifier is not None and record.verifier is not None:
                try:
                    result = asyncio.run(verifier.check())
                    record.verifier["attempts"] = [{**result, "attempt": 1}]
                except Exception as error:
                    record.verifier["error"] = f"check failed: {type(error).__name__}: {error}"
            record.seconds = round(time.monotonic() - started, 1)
            if isinstance(self._inner, VerimendAgent):
                record.agent_run = self._inner.last_run
                cost = record.agent_run.cost_usd if record.agent_run else None
                # No result: charge the whole cap, never assume it was cheaper.
                self._recorder.budget.record(cost if cost is not None else self._run_cap or 0.0)
            elif self._recorder.spec.model_calls:
                raw = session.container.exec_run(["cat", AGENT_LOG]).output
                log = raw.decode("utf-8", errors="replace")
                record.stream_log = log
                cost = parse_stream(log).cost_usd
                # No result line: charge the whole cap, never assume it was cheaper.
                self._recorder.budget.record(cost if cost is not None else self._run_cap or 0.0)


class RecordingFactory(AgentFactory):
    def __init__(self, spec: AgentSpec, recorder: Recorder, batch: dict[str, Any]) -> None:
        self._spec = spec
        self._recorder = recorder
        self._batch = batch
        names = {"none": AgentName.NONE, "reference": AgentName.SAGE}
        self._named = NamedAgentFactory(names[spec.name]) if spec.name in names else None
        self._settings = load_settings()
        if spec.model_calls:
            self._api_key = self._settings.require_api_key()

    @property
    def agent_name(self) -> AgentName:
        # SAGE must show through: the harness then passes the task id to the agent.
        if self._named:
            return self._named.agent_name
        return AgentName.CLAUDE_CODE if self._spec.name == "baseline" else AgentName.NONE

    def get_agent(self, **kwargs: Any) -> BaseAgent:
        if self._named is not None:
            check = bool(self._batch.get("with_verifier"))
            return RecordingAgent(self._named.get_agent(**kwargs), self._recorder, None, check)
        run_cap = self._recorder.budget.next_run_cap(self._recorder.per_run_usd)
        if run_cap is None:
            return RecordingAgent(None, self._recorder, None)
        if self._spec.name == "verimend":
            caps = RunCaps(max_usd=run_cap, max_steps=self._batch["max_steps"],
                           max_attempts=self._batch.get("max_attempts", 1))  # fmt: skip
            agent = VerimendAgent(
                api_key=self._api_key,
                model=self._batch["model"],
                caps=caps,
                output_chars=self._settings.tool_output_chars,
                cwd=self._settings.verimend_home / "agent-cwd",
            )
            return RecordingAgent(agent, self._recorder, run_cap)
        inner = BaselineAgent(
            api_key=self._api_key,
            model=self._batch["model"],
            run_cap_usd=run_cap,
            max_turns=self._batch["max_steps"],
        )
        return RecordingAgent(inner, self._recorder, run_cap)


def main(batch_path: Path) -> int:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    spec = get_agent(batch["agent"])
    recorder = Recorder(spec, BatchBudget(max_usd=batch["max_usd"]), batch["per_run_usd"])
    factory = RecordingFactory(spec, recorder, batch)
    stage = Path(batch["stage_dir"])
    # As ADE-bench's own CLI does: the reference agent reads solutions from the task set.
    agent_kwargs = {"dataset_path": stage / "tasks"} if spec.name == "reference" else {}
    harness = Harness(
        dataset_path=stage / "tasks",
        agent_kwargs=agent_kwargs,
        output_path=stage / "ade-output",
        run_id=batch["batch"],
        agent_factory=factory,
        task_ids=[f"{t}.base" for t in batch["task_ids"]],
        n_attempts=batch["trials"],
        n_concurrent_trials=1,
        db_type="duckdb",
        project_type="dbt",
        disable_diffs=True,
        log_level=logging.WARNING,
    )
    results = harness.run()
    grades = grades_from_ade_results(results.model_dump(mode="json"))
    meta = {
        key: batch[key]
        for key in (
            "batch",
            "set",
            "source",
            "source_commit",
            "git_sha",
            "git_dirty",
            "model",
            "trials",
            "max_usd",
            "per_run_usd",
            "max_steps",
            "task_ids",
        )
    }
    meta["max_attempts"] = batch.get("max_attempts", 1)
    meta["with_verifier"] = batch.get("with_verifier", False)
    meta["grader"] = "ade-bench"
    meta["spent_usd"] = recorder.budget.spent_usd
    summary = write_batch(
        Path(batch["results_dir"]),
        recorder.records,
        grades,
        spec,
        meta,
        expected=[(t, n) for t in batch["task_ids"] for n in range(1, batch["trials"] + 1)],
        include_tests=batch["include_tests"],
    )
    for run in summary["runs"]:
        grader = "pass" if run["grader_passed"] else "fail"
        print(
            f"{run['task']:28} trial {run['trial']}  grader={grader}"
            f"  claim={run['claim']}  ${run['dollars']:.4f}"
            + (
                f"  verifier={run['failed_check'] or 'pass'} x{run['attempts']}"
                if run["attempts"]
                else ""
            ),
            flush=True,
        )
    m = summary["metrics"]
    spent = recorder.budget.spent_usd
    print(
        f"grader pass rate {m['grader_pass_rate']}, false-success rate "
        f"{m['false_success_rate']}, spent ${spent:.4f} of ${batch['max_usd']:.2f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))

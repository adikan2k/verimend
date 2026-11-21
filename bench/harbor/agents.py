# pyright: reportMissingImports=false, reportMissingModuleSource=false
"""VeriMend and the plain-agent baseline as Harbor agents, loaded by import path.

Harbor builds each task's container, calls `setup` and `run`, then scores the trial with
the task's hidden verifier, unchanged. These agents only act in the container and write
one record per trial (bench/harbor/records.py) that bench/harbor/inner.py reads back.
Both read their caps from the batch file and share the batch budget through a ledger.
"""

import json
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from bench.claude_stream import parse_stream
from bench.harbor.ledger import Ledger
from bench.harbor.records import (
    AGENT_LOG,
    INSTALL_CLAUDE_CODE,
    WORKDIR,
    baseline_command,
    verimend_prompt,
    write_record,
)
from bench.harbor.workspace import HarborWorkspace
from bench.sealed import SealedVerifier, mentions_verifier
from verimend import __version__
from verimend.agent.run import run_agent
from verimend.config import load_settings
from verimend.hooks.caps import RunCaps
from verimend.tools.verify import CONTAINER_SOURCE, CONTAINER_WORK, WorkspaceVerifier


def container_verifier(workspace: HarborWorkspace) -> WorkspaceVerifier:
    """The verifier, uploaded into the task container and run there as its own process."""
    return WorkspaceVerifier(workspace, CONTAINER_WORK, source_dir=CONTAINER_SOURCE)


class _BatchAgent(BaseAgent):
    def __init__(self, logs_dir: Path, *args: Any, batch_file: str, **kwargs: Any) -> None:
        super().__init__(logs_dir, *args, **kwargs)
        self.batch = json.loads(Path(batch_file).read_text(encoding="utf-8"))
        self.ledger = Ledger(Path(self.batch["stage_dir"]) / "ledger.json", self.batch["max_usd"])
        self.settings = load_settings()
        self.api_key = self.settings.require_api_key()

    def version(self) -> str:
        return __version__

    def _run_cap(self) -> float | None:
        cap = self.ledger.next_run_cap(self.batch["per_run_usd"])
        if cap is None:
            write_record(self.logs_dir, stopped="batch_cap", run_cap_usd=None)
        return cap


class VerimendHarborAgent(_BatchAgent):
    @staticmethod
    def name() -> str:
        return "verimend"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass  # nothing to install: the tools act through environment.exec

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        run_cap = self._run_cap()
        if run_cap is None:
            return
        workspace = await HarborWorkspace.open(environment)
        caps = RunCaps(max_usd=run_cap, max_steps=self.batch["max_steps"],
                       max_attempts=self.batch.get("max_attempts", 1))  # fmt: skip
        run = await run_agent(
            verimend_prompt(instruction),
            workspace,
            model=self.batch["model"],
            api_key=self.api_key,
            caps=caps,
            cwd=self.settings.verimend_home / "agent-cwd",
            output_chars=self.settings.tool_output_chars,
            verifier=container_verifier(workspace),
        )
        self.ledger.record(run.cost_usd if run.cost_usd is not None else run_cap)
        write_record(self.logs_dir, run_cap_usd=run_cap, seconds=run.seconds,
                     agent_run=run.to_dict())  # fmt: skip


class BaselineHarborAgent(_BatchAgent):
    """ADE-bench's Claude Code command, run in the data-eng-bench container."""

    @staticmethod
    def name() -> str:
        return "verimend-baseline"

    async def setup(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(INSTALL_CLAUDE_CODE, timeout_sec=900)
        if result.return_code != 0:
            raise RuntimeError(f"installing Claude Code failed: {(result.stderr or '')[-500:]}")

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        run_cap = self._run_cap()
        if run_cap is None:
            return
        command = baseline_command(
            instruction, self.batch["model"], run_cap, self.batch["max_steps"]
        )
        # --with-verifier: the verifier watches and tells the baseline nothing. Sealed,
        # because the baseline has a shell in this container (bench/sealed.py).
        checked: dict[str, Any] | None = None
        verifier: SealedVerifier | None = None
        if self.batch.get("with_verifier"):
            checked = {"snapshot": None, "attempts": [], "error": None}
            verifier = SealedVerifier(container_verifier(await HarborWorkspace.open(environment)))
            try:
                checked["snapshot"] = await verifier.prepare()
            except Exception as error:
                checked["error"] = f"snapshot failed: {type(error).__name__}: {error}"[:800]
                verifier = None
        started = time.monotonic()
        await environment.exec(
            command,
            cwd=WORKDIR,
            env={"ANTHROPIC_API_KEY": self.api_key},  # this command only
            timeout_sec=int(self.batch.get("agent_timeout_sec", 1200)),
        )
        seconds = round(time.monotonic() - started, 1)
        log = (await environment.exec(f"cat {AGENT_LOG}")).stdout or ""
        cost = parse_stream(log).cost_usd
        self.ledger.record(cost if cost is not None else run_cap)
        if checked is not None:
            checked["agent_mentioned_verifier"] = mentions_verifier(log)
            if verifier is not None:
                try:
                    checked["attempts"] = [{**await verifier.check(), "attempt": 1}]
                except Exception as error:
                    checked["error"] = f"check failed: {type(error).__name__}: {error}"[:800]
        write_record(self.logs_dir, run_cap_usd=run_cap, seconds=seconds, stream_log=log,
                     verifier=checked)  # fmt: skip


class _VerifierChecked(BaseAgent):
    """A free agent (Harbor's nop or oracle) with the verifier around it: snapshot before,
    check after. Measures the verifier's false rejections; no model calls."""

    def __init__(self, logs_dir: Path, *args: Any, batch_file: str, **kwargs: Any) -> None:
        super().__init__(logs_dir, *args, **kwargs)
        self.batch = json.loads(Path(batch_file).read_text(encoding="utf-8"))

    def version(self) -> str:
        return __version__

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def act(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        raise NotImplementedError

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        started = time.monotonic()
        checked: dict[str, Any] = {"snapshot": None, "attempts": [], "error": None}
        verifier = container_verifier(await HarborWorkspace.open(environment))
        try:
            checked["snapshot"] = await verifier.prepare()
        except Exception as error:
            checked["error"] = f"snapshot failed: {type(error).__name__}: {error}"[:800]
        await self.act(instruction, environment, context)
        if checked["error"] is None:
            try:
                result = await verifier.check()
                checked["attempts"] = [{**result, "attempt": 1}]
            except Exception as error:
                checked["error"] = f"check failed: {type(error).__name__}: {error}"[:800]
        write_record(self.logs_dir, run_cap_usd=None, verifier=checked,
                     seconds=round(time.monotonic() - started, 1))  # fmt: skip


class CheckedNop(_VerifierChecked):
    @staticmethod
    def name() -> str:
        return "verimend-checked-nop"

    async def act(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        pass  # does nothing, like Harbor's nop


class CheckedOracle(_VerifierChecked):
    """Harbor's oracle, built here because Harbor gives the oracle its task folder only
    when it is loaded by name. The trial folder is named <task>__<suffix>."""

    @staticmethod
    def name() -> str:
        return "verimend-checked-oracle"

    async def act(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        from harbor.agents.oracle import OracleAgent
        from harbor.models.trial.paths import TrialPaths

        trial_dir = self.logs_dir.parent
        task = trial_dir.name.rsplit("__", 1)[0]
        oracle = OracleAgent(
            logs_dir=self.logs_dir,
            task_dir=Path(self.batch["stage_dir"]) / "tasks" / task,
            trial_paths=TrialPaths(trial_dir),
            agent_timeout_sec=float(self.batch.get("agent_timeout_sec", 1200)),
        )
        await oracle.setup(environment)
        await oracle.run(instruction, environment, context)

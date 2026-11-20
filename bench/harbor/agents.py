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
from verimend import __version__
from verimend.agent.run import run_agent
from verimend.config import load_settings
from verimend.hooks.caps import RunCaps


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
        caps = RunCaps(max_usd=run_cap, max_steps=self.batch["max_steps"], max_attempts=1)
        run = await run_agent(
            verimend_prompt(instruction),
            workspace,
            model=self.batch["model"],
            api_key=self.api_key,
            caps=caps,
            cwd=self.settings.verimend_home / "agent-cwd",
            output_chars=self.settings.tool_output_chars,
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
        write_record(self.logs_dir, run_cap_usd=run_cap, seconds=seconds, stream_log=log)

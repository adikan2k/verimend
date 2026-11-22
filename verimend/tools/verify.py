"""The agent's side of the verifier: put it in the workspace, snapshot the project before
the run, and ask it about a fix after a `fixed` report.

The verifier runs as its own process (`python -m verifier`) with its own copies of the
project and database; this module only starts it and reads its JSON. It imports nothing
from the verifier except the location of its source files, which it uploads into
containers. The agent never sees the verifier's code, only the failed check and its
message.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verimend.tools.workspace import Workspace

# Folders inside a container. Outside the project, so the agent's tools cannot reach them.
CONTAINER_SOURCE = "/tmp/verimend-verifier"
CONTAINER_WORK = "/tmp/verimend-verify"
SNAPSHOT_TIMEOUT = 3600.0
CHECK_TIMEOUT = 3600.0
DBT_TIMEOUT = 1500.0
FEEDBACK_CHARS = 1500


class VerifierUnavailable(RuntimeError):
    """The verifier could not run. A harness problem, never the agent's failure."""


def verifier_sources() -> dict[str, bytes]:
    """The verifier package's Python files, by name, for uploading into a container."""
    import verifier  # only to find the files; nothing of it runs in this process

    assert verifier.__file__ is not None
    folder = Path(verifier.__file__).parent
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.py"))}


def parse_output(code: int, output: str) -> dict[str, Any]:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                break
            if code in (0, 1) and "error" not in data:
                return data
            raise VerifierUnavailable(str(data.get("error") or f"exit {code}")[:800])
    raise VerifierUnavailable(f"no result from the verifier (exit {code}): {output[-800:]}")


@dataclass
class WorkspaceVerifier:
    """Runs the verifier inside a workspace. `source_dir` set means upload it there first
    (containers); None means it is installed next to the workspace's Python (local)."""

    workspace: Workspace
    work_dir: str
    source_dir: str | None = None
    snapshot: dict[str, Any] | None = None

    def _argv(self, *args: str) -> list[str]:
        return [
            *self.workspace.python,
            "-m",
            "verifier",
            *args,
            "--dbt",
            self.workspace.dbt[0],
            "--timeout",
            str(DBT_TIMEOUT),
        ]

    def _env(self) -> dict[str, str] | None:
        return {"PYTHONPATH": self.source_dir} if self.source_dir else None

    async def install(self) -> None:
        if self.source_dir is None:
            return
        for name, data in verifier_sources().items():
            await self.workspace.put_file(f"{self.source_dir}/verifier/{name}", data)

    async def _run(self, argv: list[str], timeout: float) -> dict[str, Any]:
        result = await self.workspace.exec(argv, env=self._env(), timeout=timeout)
        if result.timed_out:
            raise VerifierUnavailable(f"the verifier timed out after {timeout:.0f} s")
        return parse_output(result.code, result.output)

    async def prepare(self) -> dict[str, Any]:
        """Install, then snapshot. Must run before the agent changes anything."""
        await self.install()
        ws = self.workspace
        argv = self._argv(
            "snapshot", "--project-dir", ws.project_dir, "--db", ws.db_path, "--work", self.work_dir
        )
        self.snapshot = await self._run(argv, SNAPSHOT_TIMEOUT)
        return self.snapshot

    async def check(self) -> dict[str, Any]:
        """The verifier's decision on the project as it is now, against the snapshot."""
        argv = self._argv(
            "verify", "--work", self.work_dir, "--from-project", self.workspace.project_dir
        )
        return await self._run(argv, CHECK_TIMEOUT)

    async def check_diff(self, diff_text: str, name: str) -> dict[str, Any]:
        """Re-verify a recorded diff against the snapshot (bench/replay.py), never in a
        real run: `--replay` leaves the snapshot as it was and measures every check."""
        path = f"{self.work_dir}-replay/{name}.diff"
        await self.workspace.put_file(path, diff_text.encode("utf-8"))
        argv = self._argv("verify", "--work", self.work_dir, "--diff", path, "--replay")
        return await self._run(argv, CHECK_TIMEOUT)


def feedback_text(result: dict[str, Any], attempt: int, max_attempts: int, detail: bool) -> str:
    """What the agent hears after a rejection: which check failed and its message."""
    head = f"The verifier rejected the fix (attempt {attempt} of {max_attempts})."
    if not detail:
        return head + " Fix the problem and call report again."
    failing = next((c for c in result.get("checks", []) if not c.get("passed")), {})
    message = str(failing.get("detail") or "")[:FEEDBACK_CHARS]
    return (
        f"{head}\nFailed check: {result.get('failed_check')}\n{message}\n"
        "Find the cause, fix it, rebuild, and call report again."
    )

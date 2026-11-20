"""Where the tools act: a dbt project and its DuckDB file, on the host or in a container.

The tools only ever talk to a `Workspace`, so the same tool code runs against a local
sandbox (tests now, `verimend fix` in phase 4), an ADE-bench task container and a Harbor
task container. The container workspaces live in the bench adapters; this module holds
the protocol, path safety and the local implementation.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

# Folders a directory listing leaves out unless the path asks for them explicitly.
NOISE_DIRS = ("target", "logs", "dbt_packages", ".git", "__pycache__", ".venv")


@dataclass(frozen=True)
class ExecResult:
    code: int
    output: str  # stdout and stderr together
    timed_out: bool = False


class Workspace(Protocol):
    """A dbt project the tools can read, change, query and build."""

    @property
    def project_dir(self) -> str:
        """Absolute POSIX path of the project inside the workspace."""
        ...

    @property
    def db_path(self) -> str:
        """Absolute path of the DuckDB file the project builds into."""
        ...

    @property
    def python(self) -> list[str]:
        """Command that runs a Python with `duckdb` importable."""
        ...

    @property
    def dbt(self) -> list[str]:
        """Command that runs dbt for this project."""
        ...

    async def exec(
        self, argv: list[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None
    ) -> ExecResult:
        """Run a command with the project as the working directory."""
        ...

    async def read_text(self, rel: str) -> str | None:
        """A project file's text, or None if it does not exist or is a folder."""
        ...

    async def write_text(self, rel: str, text: str) -> None: ...

    async def remove(self, rel: str) -> None:
        """Delete a project file if it exists."""
        ...

    async def list_files(self, rel: str, max_depth: int) -> list[str] | None:
        """Project-relative paths under a folder, or None if it is not a folder."""
        ...

    async def put_file(self, path: str, data: bytes) -> None:
        """Write a file at an absolute path outside the project (the verifier's code).
        Never used by the agent's tools."""
        ...


def safe_relative(path: str, project_dir: str) -> str | None:
    """A project-relative POSIX path, or None if the path leaves the project.

    Accepts paths relative to the project root and absolute paths inside it (benchmark
    prompts name files by absolute path). Rejects `..`, other absolute paths and drives.
    """
    text = path.strip().replace("\\", "/")
    root = project_dir.rstrip("/")
    if text == root:
        return "."
    if text.startswith(root + "/"):
        text = text[len(root) + 1 :]
    while text.startswith("./"):
        text = text[2:]
    if not text or text == ".":
        return "."
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or ":" in candidate.parts[0] or ".." in candidate.parts:
        return None
    return candidate.as_posix()


class ShellWorkspace:
    """A project inside a Linux container, reached only by running commands in it.

    Subclasses supply two primitives: `run` (a command and its exit code and output) and
    `upload` (bytes to an absolute path). Everything else is POSIX tools: cat, rm, find.
    """

    def __init__(self, project_dir: str, db_path: str, python: str = "python3", dbt: str = "dbt"):
        self._project_dir = project_dir.rstrip("/")
        self._db_path = db_path
        self._python = [python]
        self._dbt = [dbt]

    @property
    def project_dir(self) -> str:
        return self._project_dir

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def python(self) -> list[str]:
        return self._python

    @property
    def dbt(self) -> list[str]:
        return self._dbt

    async def run(self, argv: list[str], env: Mapping[str, str] | None) -> tuple[int, str]:
        raise NotImplementedError

    async def upload(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    def _abs(self, rel: str) -> str:
        relative = safe_relative(rel, self._project_dir)
        if relative is None:
            raise ValueError(f"path leaves the project: {rel!r}")
        return self._project_dir if relative == "." else f"{self._project_dir}/{relative}"

    async def exec(
        self, argv: list[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None
    ) -> ExecResult:
        command = ["timeout", str(max(int(timeout), 1)), *argv] if timeout else argv
        code, output = await self.run(command, env)
        return ExecResult(code, output, timed_out=bool(timeout) and code == 124)

    async def read_text(self, rel: str) -> str | None:
        path = self._abs(rel)
        code, output = await self.run(["sh", "-c", 'test -f "$1" && cat "$1"', "sh", path], None)
        return output if code == 0 else None

    async def write_text(self, rel: str, text: str) -> None:
        path = self._abs(rel)
        await self.run(["mkdir", "-p", str(PurePosixPath(path).parent)], None)
        await self.upload(path, text.encode("utf-8"))

    async def remove(self, rel: str) -> None:
        await self.run(["rm", "-f", self._abs(rel)], None)

    async def put_file(self, path: str, data: bytes) -> None:
        await self.run(["mkdir", "-p", str(PurePosixPath(path).parent)], None)
        await self.upload(path, data)

    async def list_files(self, rel: str, max_depth: int) -> list[str] | None:
        base = self._abs(rel)
        script = (
            'test -d "$1" || exit 3; cd "$1" && find . -mindepth 1 -maxdepth "$2" '
            + "\\( "
            + " -o ".join(f"-name {n}" for n in NOISE_DIRS)
            + " \\) -prune -o -type f -print"
        )
        code, output = await self.run(["sh", "-c", script, "sh", base, str(max_depth)], None)
        if code == 3:
            return None
        prefix = "" if base == self._project_dir else base[len(self._project_dir) + 1 :] + "/"
        return sorted(prefix + line[2:] for line in output.splitlines() if line.startswith("./"))


def _local_dbt() -> str:
    """The dbt executable installed next to this Python."""
    name = "dbt.exe" if os.name == "nt" else "dbt"
    return str(Path(sys.executable).parent / name)


@dataclass
class LocalWorkspace:
    """A project folder on this machine. Used by the tests and, in phase 4, the CLI's
    sandbox copy. Never point it at a project you do not want changed."""

    root: Path
    db_file: Path
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def project_dir(self) -> str:
        return self.root.resolve().as_posix()

    @property
    def db_path(self) -> str:
        return self.db_file.resolve().as_posix()

    @property
    def python(self) -> list[str]:
        return [sys.executable]

    @property
    def dbt(self) -> list[str]:
        return [_local_dbt()]

    def _path(self, rel: str) -> Path:
        relative = safe_relative(rel, self.project_dir)
        if relative is None:
            raise ValueError(f"path leaves the project: {rel!r}")
        return self.root / relative

    async def exec(
        self, argv: list[str], *, env: Mapping[str, str] | None = None, timeout: float | None = None
    ) -> ExecResult:
        full_env = {
            **os.environ,
            "DBT_PROFILES_DIR": str(self.root),
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
            "DO_NOT_TRACK": "1",
            **self.extra_env,
            **(env or {}),
        }

        def run() -> ExecResult:
            try:
                done = subprocess.run(
                    argv,
                    cwd=self.root,
                    env=full_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return ExecResult(124, "", timed_out=True)
            except OSError as error:
                return ExecResult(127, str(error))
            return ExecResult(done.returncode, (done.stdout or "") + (done.stderr or ""))

        return await asyncio.to_thread(run)

    async def read_text(self, rel: str) -> str | None:
        path = self._path(rel)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    async def write_text(self, rel: str, text: str) -> None:
        path = self._path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    async def remove(self, rel: str) -> None:
        self._path(rel).unlink(missing_ok=True)

    async def put_file(self, path: str, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_files(self, rel: str, max_depth: int) -> list[str] | None:
        base = self._path(rel)
        if not base.is_dir():
            return None
        found: list[str] = []
        for current, dirs, files in os.walk(base):
            depth = len(Path(current).relative_to(base).parts)
            dirs[:] = sorted(d for d in dirs if depth + 1 <= max_depth and d not in NOISE_DIRS)
            for name in sorted(files):
                found.append((Path(current) / name).relative_to(self.root).as_posix())
        return sorted(found)

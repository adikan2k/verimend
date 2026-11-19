"""VeriMend's workspace inside an ADE-bench task container, through the Docker API the
harness already holds (the phase 1 pilot's `Container`, made a Workspace)."""

import asyncio
import io
import tarfile
import time
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from verimend.tools.workspace import ShellWorkspace

APP = "/app"  # ADE-bench copies the project and its DuckDB file here


class AdeWorkspace(ShellWorkspace):
    def __init__(self, container: Any, db_path: str) -> None:
        super().__init__(APP, db_path)
        self._container = container

    @classmethod
    def open(cls, container: Any) -> "AdeWorkspace":
        """Find the task's DuckDB file: ADE-bench puts exactly one in /app."""
        result = container.exec_run(["sh", "-c", f"ls {APP}/*.duckdb"], workdir=APP)
        found = result.output.decode("utf-8", errors="replace").split()
        return cls(container, found[0] if found else f"{APP}/database.duckdb")

    async def run(self, argv: list[str], env: Mapping[str, str] | None) -> tuple[int, str]:
        def go() -> tuple[int, str]:
            result = self._container.exec_run(
                argv, workdir=self.project_dir, environment=dict(env) if env else None
            )
            return result.exit_code, result.output.decode("utf-8", errors="replace")

        return await asyncio.to_thread(go)

    async def upload(self, path: str, data: bytes) -> None:
        target = PurePosixPath(path)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo(name=target.name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        await asyncio.to_thread(self._container.put_archive, str(target.parent), stream.getvalue())

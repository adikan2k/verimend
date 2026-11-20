"""VeriMend's workspace inside a Harbor task container, through Harbor's environment API
(`exec` and `upload_file`, read from Harbor 0.26's BaseEnvironment)."""

import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bench.harbor.records import DEFAULT_DUCKDB_PATH, PROJECT_DIR
from verimend.tools.workspace import ShellWorkspace


class HarborWorkspace(ShellWorkspace):
    def __init__(self, environment: Any, project_dir: str, db_path: str) -> None:
        super().__init__(project_dir, db_path)
        self._environment = environment

    @classmethod
    async def open(cls, environment: Any, project_dir: str = PROJECT_DIR) -> "HarborWorkspace":
        result = await environment.exec("printenv DUCKDB_PATH")
        db_path = (result.stdout or "").strip() or DEFAULT_DUCKDB_PATH
        return cls(environment, project_dir, db_path)

    async def run(self, argv: list[str], env: Mapping[str, str] | None) -> tuple[int, str]:
        result = await self._environment.exec(
            shlex.join(argv), cwd=self.project_dir, env=dict(env) if env else None
        )
        return int(result.return_code), (result.stdout or "") + (result.stderr or "")

    async def upload(self, path: str, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "upload"
            source.write_bytes(data)
            await self._environment.upload_file(source, path)

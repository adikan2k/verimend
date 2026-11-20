"""ShellWorkspace, the base of the container workspaces, against a fake container."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from verimend.tools.workspace import ShellWorkspace


class FakeContainer(ShellWorkspace):
    def __init__(self, replies: dict[str, tuple[int, str]] | None = None) -> None:
        super().__init__("/app/proj", "/app/db.duckdb")
        self.calls: list[tuple[list[str], Mapping[str, str] | None]] = []
        self.uploads: dict[str, bytes] = {}
        self.replies = replies or {}

    async def run(self, argv: list[str], env: Mapping[str, str] | None) -> tuple[int, str]:
        self.calls.append((argv, env))
        for key, reply in self.replies.items():
            if key in " ".join(argv):
                return reply
        return 0, ""

    async def upload(self, path: str, data: bytes) -> None:
        self.uploads[path] = data


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_exec_adds_a_timeout_and_reports_it() -> None:
    ws = FakeContainer({"dbt build": (124, "")})
    result = run(ws.exec(["dbt", "build"], timeout=900))
    assert ws.calls[0][0] == ["timeout", "900", "dbt", "build"]
    assert result.timed_out
    assert not run(ws.exec(["dbt", "parse"])).timed_out
    assert ws.calls[1][0] == ["dbt", "parse"]


def test_paths_are_absolute_inside_the_project() -> None:
    ws = FakeContainer({"cat": (0, "select 1")})
    assert run(ws.read_text("models/a.sql")) == "select 1"
    assert ws.calls[0][0][-1] == "/app/proj/models/a.sql"
    run(ws.write_text("models/new/b.sql", "select 2"))
    assert ws.calls[-1][0] == ["mkdir", "-p", "/app/proj/models/new"]
    assert ws.uploads == {"/app/proj/models/new/b.sql": b"select 2"}
    run(ws.remove("target/run_results.json"))
    assert ws.calls[-1][0] == ["rm", "-f", "/app/proj/target/run_results.json"]
    with pytest.raises(ValueError):
        run(ws.read_text("../../etc/passwd"))


def test_read_text_of_a_missing_file_is_none() -> None:
    ws = FakeContainer({"cat": (1, "")})
    assert run(ws.read_text("models/nope.sql")) is None


def test_list_files_returns_project_relative_paths() -> None:
    found = "./staging/stg.sql\n./marts/m.sql\n"
    ws = FakeContainer({"find": (0, found)})
    assert run(ws.list_files("models", 3)) == ["models/marts/m.sql", "models/staging/stg.sql"]
    argv = ws.calls[0][0]
    assert argv[:2] == ["sh", "-c"] and argv[-2:] == ["/app/proj/models", "3"]
    assert "-name target" in argv[2] and "-prune" in argv[2]
    assert run(FakeContainer({"find": (3, "")}).list_files("models/a.sql", 3)) is None
    root = FakeContainer({"find": (0, "./dbt_project.yml\n")})
    assert run(root.list_files(".", 1)) == ["dbt_project.yml"]

"""bench/sealed.py: a snapshot the agent changed is an error, never a verdict. The seal
script runs for real, in the shell the containers have."""

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench.sealed import SealedVerifier, SnapshotModified, mentions_verifier
from verimend.tools.workspace import ExecResult

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")


class ShellWorkspace:
    async def exec(self, argv: list[str], *, env: Any = None, timeout: Any = None) -> ExecResult:
        done = subprocess.run(argv, capture_output=True, text=True)
        return ExecResult(done.returncode, done.stdout + done.stderr)


class StubVerifier:
    """Stands in for WorkspaceVerifier: a work folder with what `verify` reads."""

    def __init__(self, work: Path) -> None:
        self.workspace = ShellWorkspace()
        self.work_dir = work.as_posix()
        self.calls: list[str] = []
        (work / "base" / "project" / "models").mkdir(parents=True)
        (work / "base" / "db").mkdir()
        (work / "base" / "project" / "models" / "orders.sql").write_text("select 1\n")
        (work / "base" / "db" / "shop.duckdb").write_bytes(b"\x00database")
        (work / "base" / "manifest.json").write_text("{}")
        (work / "state.json").write_text('{"stats": {}}')

    async def install(self) -> None:
        self.calls.append("install")

    async def prepare(self) -> dict[str, Any]:
        self.calls.append("prepare")
        return {"ok": True}

    async def check(self) -> dict[str, Any]:
        self.calls.append("check")
        return {"passed": True}


def sealed(tmp_path: Path) -> tuple[SealedVerifier, StubVerifier, Path]:
    work = tmp_path / "verify"
    stub = StubVerifier(work)
    verifier = SealedVerifier(stub)  # type: ignore[arg-type]
    assert asyncio.run(verifier.prepare()) == {"ok": True}
    return verifier, stub, work


def test_an_untouched_snapshot_is_checked_with_freshly_uploaded_code(tmp_path: Path) -> None:
    verifier, stub, work = sealed(tmp_path)
    (work / "base-build").mkdir()  # the verifier's own scratch folders are not sealed
    (work / "base-build" / "anything").write_text("x")
    assert asyncio.run(verifier.check()) == {"passed": True}
    assert stub.calls == ["prepare", "install", "check"]


@pytest.mark.parametrize(
    "tamper",
    [
        lambda w: (w / "state.json").write_text('{"stats": {"x": 1}}'),
        lambda w: (w / "base" / "manifest.json").write_text('{"nodes": {}}'),
        lambda w: (w / "base" / "project" / "models" / "orders.sql").write_text("select 2\n"),
        lambda w: (w / "base" / "db" / "shop.duckdb").write_bytes(b"\x00other"),
        lambda w: (w / "base" / "project" / "models" / "new.sql").write_text("select 3\n"),
        lambda w: (w / "base" / "manifest.json").unlink(),
    ],
    ids=["state", "manifest", "model", "database", "added file", "removed file"],
)
def test_a_changed_snapshot_is_an_error_and_is_never_checked(tmp_path: Path, tamper: Any) -> None:
    verifier, stub, work = sealed(tmp_path)
    tamper(work)
    with pytest.raises(SnapshotModified, match="modified"):
        asyncio.run(verifier.check())
    assert "check" not in stub.calls


def test_a_check_without_a_snapshot_is_refused(tmp_path: Path) -> None:
    verifier = SealedVerifier(StubVerifier(tmp_path / "verify"))  # type: ignore[arg-type]
    with pytest.raises(SnapshotModified, match="no sealed snapshot"):
        asyncio.run(verifier.check())


def test_mentions_verifier_spots_the_folders_in_the_agents_output() -> None:
    assert mentions_verifier('{"command": "ls /tmp/verimend-verify/base"}')
    assert mentions_verifier("cat /tmp/verimend-verifier/verifier/checks.py")
    assert not mentions_verifier('{"command": "ls /tmp && dbt build"}')
    assert not mentions_verifier(None)

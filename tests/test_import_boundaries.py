"""CLAUDE.md rule 5: the verifier never imports verimend or the Agent SDK, and inside
verimend only the agent package imports the Agent SDK."""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SDK = "claude_agent_sdk"


def imported_modules(source: str) -> set[str]:
    """Top-level names of every module a source file imports."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def violations(root: Path, banned: set[str], skip: tuple[str, ...] = ()) -> list[str]:
    """Files under root that import a banned module. `skip` holds sub-folders to leave out."""
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in skip):
            continue
        hits = imported_modules(path.read_text(encoding="utf-8")) & banned
        bad.extend(f"{relative} imports {name}" for name in sorted(hits))
    return bad


def test_checker_catches_a_banned_import(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("import json\nfrom pathlib import Path\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("from verimend.config import x\n", encoding="utf-8")
    (tmp_path / "worse.py").write_text(f"import {SDK}.types\n", encoding="utf-8")
    assert violations(tmp_path, {"verimend", SDK}) == [
        "bad.py imports verimend",
        f"worse.py imports {SDK}",
    ]


def test_checker_honours_skip(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "loop.py").write_text(f"import {SDK}\n", encoding="utf-8")
    assert violations(tmp_path, {SDK}, skip=("agent/",)) == []
    assert violations(tmp_path, {SDK}) == [f"agent/loop.py imports {SDK}"]


def test_verifier_never_imports_verimend_or_the_sdk() -> None:
    assert violations(REPO / "verifier", {"verimend", SDK}) == []


def test_only_the_agent_package_imports_the_sdk() -> None:
    assert violations(REPO / "verimend", {SDK}, skip=("agent/",)) == []

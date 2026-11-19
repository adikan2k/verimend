"""scripts/check.py is the gate before every commit, so prove it reports a failure."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check.py"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_failing_command_is_reported_as_failed() -> None:
    check = load_check()
    results = check.run(
        [
            ("passes", [sys.executable, "-c", "raise SystemExit(0)"]),
            ("fails", [sys.executable, "-c", "raise SystemExit(3)"]),
        ]
    )
    assert [(name, passed) for name, passed, _ in results] == [("passes", True), ("fails", False)]


def test_main_exits_non_zero_when_any_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    check = load_check()
    failing = [("fails", [sys.executable, "-c", "raise SystemExit(1)"])]
    monkeypatch.setattr(check, "python_checks", lambda: failing)
    monkeypatch.setattr(check, "site_checks", lambda: [])
    assert check.main() == 1


def test_site_checks_are_skipped_until_the_site_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    check = load_check()
    monkeypatch.setattr(check, "SITE", tmp_path / "site")
    assert check.site_checks() == []

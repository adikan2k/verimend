"""A small dbt runner for the fault suite: building faults, checking them, grading runs.

The grader must not share code with the verifier (a verifier bug must not hide in the
grade), so the suite has its own runner instead of verifier.project.Dbt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE = "jaffle_shop"
TARGET = "dev"
FAILED = frozenset({"error", "fail", "runtime error"})


def dbt_exe() -> str:
    """The dbt next to this Python, as the uv environment installs it."""
    name = "dbt.exe" if os.name == "nt" else "dbt"
    beside = Path(sys.executable).parent / name
    return str(beside) if beside.is_file() else (shutil.which("dbt") or "dbt")


def write_profile(db: Path, out_dir: Path) -> Path:
    """A profiles.yml for the demo project's profile, pointed at `db`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"{PROFILE}:\n  target: {TARGET}\n  outputs:\n    {TARGET}:\n"
        f"      type: duckdb\n      path: '{db.resolve().as_posix()}'\n      threads: 4\n"
    )
    (out_dir / "profiles.yml").write_text(text, "utf-8")
    return out_dir


@dataclass
class Build:
    code: int
    output: str
    statuses: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None

    @property
    def parsed(self) -> bool:
        return self.manifest is not None

    @property
    def failing(self) -> list[str]:
        """The nodes that failed, or ["parse"] when dbt could not parse the project."""
        if not self.parsed:
            return ["parse"]
        return sorted(uid for uid, status in self.statuses.items() if status in FAILED)

    @property
    def passed(self) -> bool:
        return self.parsed and self.code == 0 and not self.failing


def run_dbt(project: Path, profiles_dir: Path, *args: str, timeout: float = 900) -> Build:
    """Run dbt on `project` from a clean target folder and read what it wrote."""
    shutil.rmtree(project / "target", ignore_errors=True)
    env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "False"}
    for name in ("DBT_PROFILES_DIR", "DBT_TARGET_PATH", "DBT_PROJECT_DIR"):
        env.pop(name, None)
    command = [dbt_exe(), *args, "--project-dir", str(project),
               "--profiles-dir", str(profiles_dir), "--target", TARGET]  # fmt: skip
    done = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)  # fmt: skip
    output = done.stdout + done.stderr
    manifest = _read_json(project / "target" / "manifest.json")
    results = _read_json(project / "target" / "run_results.json") or {}
    statuses = {r["unique_id"]: str(r.get("status")) for r in results.get("results", [])}
    return Build(done.returncode, output, statuses, manifest)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

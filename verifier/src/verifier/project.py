"""The parts of a dbt project the verifier needs: its profile, its folders, and dbt itself.

The verifier never builds against the real database. It copies the project and the
DuckDB file, then writes its own `profiles.yml` that points the project's own profile
and target at the copy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from verifier.patch import SKIP_DIRS, SKIP_FILES, SKIP_SUFFIXES

# Statuses in run_results.json that mean a node did not build or a test did not pass.
FAILED_STATUSES = frozenset({"error", "fail", "skipped", "runtime error"})
BUILT_TYPES = ("model", "seed", "snapshot")


class VerifierError(RuntimeError):
    """The verifier could not run at all (as opposed to a check failing)."""


@dataclass(frozen=True)
class Profile:
    name: str
    target: str
    output: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "target": self.target, "output": self.output}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Profile:
        return cls(raw["name"], raw["target"], raw["output"])


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VerifierError(f"cannot read {path.name}: {exc}") from exc


def find_profiles_dir(project_dir: Path, profiles_dir: Path | None) -> Path:
    candidates = [profiles_dir] if profiles_dir else []
    candidates.append(project_dir)
    if os.environ.get("DBT_PROFILES_DIR"):
        candidates.append(Path(os.environ["DBT_PROFILES_DIR"]))
    candidates.append(Path.home() / ".dbt")
    for candidate in candidates:
        if candidate is not None and (candidate / "profiles.yml").is_file():
            return candidate
    raise VerifierError("no profiles.yml found (pass --profiles-dir)")


def read_profile(project_dir: Path, profiles_dir: Path, target: str | None) -> Profile:
    project = load_yaml(project_dir / "dbt_project.yml")
    if not isinstance(project, dict) or not project.get("profile"):
        raise VerifierError("dbt_project.yml names no profile")
    name = str(project["profile"])
    profiles = load_yaml(profiles_dir / "profiles.yml")
    if not isinstance(profiles, dict) or name not in profiles:
        raise VerifierError(f"profile {name!r} is not in profiles.yml")
    profile = profiles[name]
    chosen = target or str(profile.get("target", ""))
    if not chosen or "{{" in chosen:
        raise VerifierError(f"cannot tell the target of profile {name!r} (pass --target)")
    output = (profile.get("outputs") or {}).get(chosen)
    if not isinstance(output, dict):
        raise VerifierError(f"profile {name!r} has no output {chosen!r}")
    if output.get("type") != "duckdb":
        raise VerifierError(f"only DuckDB projects are supported, not {output.get('type')!r}")
    return Profile(name, chosen, dict(output))


def write_profiles(profile: Profile, db_path: Path, out_dir: Path) -> Path:
    """A profiles.yml with only this profile and target, pointed at `db_path`."""
    output = dict(profile.output)
    output["path"] = db_path.resolve().as_posix()
    out_dir.mkdir(parents=True, exist_ok=True)
    content = {profile.name: {"target": profile.target, "outputs": {profile.target: output}}}
    (out_dir / "profiles.yml").write_text(yaml.safe_dump(content, sort_keys=True), "utf-8")
    return out_dir


def project_paths(project_dir: Path) -> dict[str, list[str]]:
    """The folders dbt reads each kind of file from, defaults included."""
    project = load_yaml(project_dir / "dbt_project.yml") or {}
    defaults = {
        "model-paths": ["models"],
        "macro-paths": ["macros"],
        "seed-paths": ["seeds"],
        "snapshot-paths": ["snapshots"],
        "analysis-paths": ["analyses"],
        "test-paths": ["tests"],
    }
    return {
        key: [str(p).strip("/") for p in (project.get(key) or default)]
        for key, default in defaults.items()
    }


def project_vars(project_dir: Path) -> Any:
    project = load_yaml(project_dir / "dbt_project.yml") or {}
    return project.get("vars")


def copy_project(source: Path, dest: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in SKIP_DIRS or n in SKIP_FILES or n.endswith(SKIP_SUFFIXES)}

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=ignore)


def copy_database(source: Path, dest_dir: Path) -> Path:
    """Copy the DuckDB file, keeping its name: DuckDB names the catalog after the file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if source.is_file():
        shutil.copy2(source, dest)
        wal = source.with_name(source.name + ".wal")
        if wal.is_file():
            shutil.copy2(wal, dest.with_name(dest.name + ".wal"))
    return dest


def default_dbt() -> str:
    name = "dbt.exe" if os.name == "nt" else "dbt"
    sibling = Path(sys.executable).parent / name
    return str(sibling) if sibling.is_file() else "dbt"


@dataclass(frozen=True)
class Dbt:
    executable: str
    profiles_dir: Path
    target: str
    timeout: float = 1500.0

    def run(self, project_dir: Path, *args: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "False"
        env.pop("DBT_TARGET_PATH", None)
        env.pop("DBT_PROFILES_DIR", None)
        command = [
            self.executable,
            *args,
            "--project-dir",
            str(project_dir),
            "--profiles-dir",
            str(self.profiles_dir),
            "--target",
            self.target,
        ]
        try:
            done = subprocess.run(
                command,
                cwd=project_dir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"dbt {' '.join(args)} timed out after {self.timeout:.0f} s"
        except OSError as exc:
            raise VerifierError(f"cannot run dbt: {exc}") from exc
        return done.returncode, (done.stdout or "") + (done.stderr or "")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def node_statuses(project_dir: Path) -> dict[str, dict[str, str]]:
    results = read_json(project_dir / "target" / "run_results.json") or {}
    statuses: dict[str, dict[str, str]] = {}
    for result in results.get("results", []):
        statuses[result["unique_id"]] = {
            "status": str(result.get("status")),
            "message": str(result.get("message") or "")[:300],
        }
    return statuses


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def table_stats(
    db_path: Path, manifest: dict[str, Any], statuses: dict[str, dict[str, str]]
) -> dict[str, list[str | int] | None]:
    """Row count and an order-independent checksum of every model, seed and snapshot
    that built. `sum(hash(t))` over the row struct does not depend on row order."""
    import duckdb  # imported here so the rest of the package loads without it

    stats: dict[str, list[str | int] | None] = {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for uid, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") not in BUILT_TYPES:
                continue
            if (node.get("config") or {}).get("materialized") == "ephemeral":
                continue
            if statuses.get(uid, {}).get("status") not in ("success", "pass", "warn"):
                continue
            relation = f"{_quote(node['schema'])}.{_quote(node.get('alias') or node['name'])}"
            try:
                row = con.execute(
                    f"select count(*), coalesce(sum(hash(t)), 0)::varchar from {relation} as t"
                ).fetchone()
                stats[uid] = [int(row[0]), str(row[1])] if row else None
            except duckdb.Error:
                stats[uid] = None
    finally:
        con.close()
    return stats

"""Snapshot a project before anyone touches it, then verify a diff against that snapshot.

Work folder layout:
    base/project/      the project as it was, never built in place
    base/db/<file>     the database as it was, never opened for writing
    base/manifest.json the manifest of the base build
    state.json         profile, folders, vars, and the base build's statuses and stats
    base-build/        where the base was built (a copy)
    runs/<n>/          one fresh copy per verification
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from verifier import checks
from verifier.patch import DiffError, FilePatch, apply_diff, make_diff, parse_diff
from verifier.project import (
    FAILED_STATUSES,
    Dbt,
    Profile,
    VerifierError,
    copy_database,
    copy_project,
    find_profiles_dir,
    node_statuses,
    project_paths,
    project_vars,
    read_json,
    read_profile,
    table_stats,
    write_profiles,
)


def _build_copy(
    work: Path, name: str, state: dict[str, Any], dbt_exe: str, timeout: float
) -> tuple[Path, Dbt]:
    """A fresh copy of the base project and database under work/<name>."""
    root = work / name
    if root.exists():
        shutil.rmtree(root)
    project = root / "project"
    copy_project(work / "base" / "project", project)
    db = copy_database(work / "base" / "db" / state["db_name"], root / "db")
    profiles = write_profiles(Profile.from_dict(state["profile"]), db, root / "profiles")
    return project, Dbt(dbt_exe, profiles, state["profile"]["target"], timeout)


def _needs_deps(project: Path) -> bool:
    has_packages = (project / "packages.yml").is_file() or (project / "dependencies.yml").is_file()
    return has_packages and not (project / "dbt_packages").is_dir()


def snapshot(
    project_dir: Path,
    db_path: Path,
    work: Path,
    *,
    profiles_dir: Path | None = None,
    target: str | None = None,
    dbt_exe: str,
    timeout: float = 1500.0,
) -> dict[str, Any]:
    started = time.monotonic()
    project_dir = project_dir.resolve()
    profile = read_profile(project_dir, find_profiles_dir(project_dir, profiles_dir), target)
    if work.exists():
        shutil.rmtree(work)
    copy_project(project_dir, work / "base" / "project")
    copy_database(db_path, work / "base" / "db")
    state: dict[str, Any] = {
        "profile": profile.to_dict(),
        "db_name": db_path.name,
        "paths": project_paths(project_dir),
        "vars": project_vars(project_dir),
    }
    base_project = work / "base" / "project"
    if _needs_deps(base_project):
        deps_dbt = Dbt(
            dbt_exe,
            write_profiles(profile, work / "deps.duckdb", work / "deps-profiles"),
            profile.target,
            timeout,
        )
        code, output = deps_dbt.run(base_project, "deps")
        if code != 0:
            raise VerifierError(f"dbt deps failed on the base project: {output[-600:]}")
    project, dbt = _build_copy(work, "base-build", state, dbt_exe, timeout)
    code, output = dbt.run(project, "build")
    statuses = node_statuses(project)
    manifest = read_json(project / "target" / "manifest.json")
    # A project that does not even parse is a real failed run (a compile error can be the
    # bug). Then there is no base manifest and no base build: the checks fall back to the
    # base's source files, and collateral has nothing to compare against.
    state["base_parsed"] = manifest is not None
    if manifest is not None:
        (work / "base" / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    state["statuses"] = statuses
    state["stats"] = (
        table_stats(project.parent / "db" / state["db_name"], manifest, statuses)
        if manifest is not None
        else {}
    )
    (work / "state.json").write_text(json.dumps(state, indent=1), "utf-8")
    failures = sorted(uid for uid, s in statuses.items() if s["status"] in FAILED_STATUSES)
    return {
        "ok": True,
        "base_parsed": state["base_parsed"],
        "base_parse_error": None if manifest is not None else output.strip()[-600:],
        "base_failures": failures,
        "nodes": len(statuses),
        "duration_s": round(time.monotonic() - started, 1),
    }


def diff_from_project(work: Path, project_dir: Path) -> str:
    """The verifier's own diff of a project against the snapshot: it never takes the
    agent's word for what changed."""
    return make_diff(work / "base" / "project", project_dir)


def _result(found: list[checks.Check], started: float, failed: str | None = None) -> dict:
    failing = failed or next((c.name for c in found if not c.passed), None)
    return {
        "passed": failing is None,
        "failed_check": failing,
        "checks": [c.to_dict() for c in found],
        "duration_s": round(time.monotonic() - started, 1),
    }


def verify(work: Path, diff_text: str, *, dbt_exe: str, timeout: float = 1500.0) -> dict:
    started = time.monotonic()
    state = read_json(work / "state.json")
    base_manifest = read_json(work / "base" / "manifest.json")
    if state is None or (base_manifest is None and state.get("base_parsed", True)):
        raise VerifierError(f"no snapshot in {work} (run snapshot first)")
    base_project = work / "base" / "project"

    if not diff_text.strip():
        empty = checks.Check("empty_diff", False, "The diff changes nothing.")
        return _result([empty], started)
    try:
        patches: list[FilePatch] = parse_diff(diff_text)
    except DiffError as exc:
        return _result([checks.Check("diff", False, f"The diff cannot be read: {exc}")], started)
    if not patches:
        return _result([checks.Check("diff", False, "The diff names no files.")], started)

    found = [checks.tamper_paths(patches, state["paths"])]
    if not found[-1].passed:
        return _result(found, started)

    runs = work / "runs"
    number = len(list(runs.iterdir())) + 1 if runs.is_dir() else 1
    project, dbt = _build_copy(work, f"runs/{number}", state, dbt_exe, timeout)
    try:
        apply_diff(project, patches)
    except DiffError as exc:
        found.append(checks.Check("diff", False, f"The diff does not apply: {exc}"))
        return _result(found, started)

    changed_paths = {p.path for p in patches}
    if "packages.yml" in changed_paths or _needs_deps(project):
        code, output = dbt.run(project, "deps")
        if code != 0:
            found.append(checks.Check("clean_rebuild", False, f"dbt deps failed: {output[-600:]}"))
            return _result(found, started)

    code, output = dbt.run(project, "parse")
    new_manifest = read_json(project / "target" / "manifest.json")
    if code != 0 or new_manifest is None:
        tail = "\n".join(output.strip().splitlines()[-15:])
        found.append(checks.Check("clean_rebuild", False, f"dbt parse failed: {tail}"[:1500]))
        return _result(found, started)

    # Tamper has two halves, the paths and the tests; report them as one check.
    if base_manifest is not None:
        tests = checks.tamper_tests(base_manifest, new_manifest)
    else:
        tests = checks.tamper_tests_from_source(base_project, project, state["paths"])
    found[0] = checks.Check("tamper", tests.passed, f"{found[0].detail}; {tests.detail}")
    if not tests.passed:
        found[0].detail = tests.detail
        return _result(found, started)
    if base_manifest is not None:
        found.append(checks.masking(base_manifest, new_manifest, patches))
    else:
        found.append(
            checks.masking_from_source(base_project, new_manifest, patches, state["paths"])
        )
    if not found[-1].passed:
        return _result(found, started)

    code, output = dbt.run(project, "build")
    statuses = node_statuses(project)
    found.append(checks.clean_rebuild(code, statuses, output))
    if not found[-1].passed:
        return _result(found, started)

    if base_manifest is None:
        found.append(checks.Check("collateral", True, checks.NO_BASE_BUILD))
        return _result(found, started)
    built_manifest = read_json(project / "target" / "manifest.json") or new_manifest
    vars_changed = project_vars(project) != state.get("vars")
    changed = checks.changed_nodes(base_manifest, built_manifest, vars_changed)
    in_lineage = checks.lineage(changed, base_manifest, built_manifest)
    new_stats = table_stats(project.parent / "db" / state["db_name"], built_manifest, statuses)
    found.append(checks.collateral(state["stats"], new_stats, in_lineage))
    return _result(found, started)

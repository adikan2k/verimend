"""Snapshot a project before anyone touches it, then verify a diff against that snapshot.

Work folder layout:
    base/project/      the project as it was, never built in place
    base/db/<file>     the database as it was, never opened for writing
    base/manifest.json the manifest of the base build
    state.json         profile, folders, vars, and the base build's statuses and stats
    base-build/        where the base was built (a copy)
    runs/<n>/          one fresh copy per verification (runs/replay with --replay, removed after)
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from verifier import accounting, checks
from verifier.patch import DiffError, FilePatch, apply_diff, make_diff, parse_diff
from verifier.policy import FIX, Policy
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


PARSE_TRIES = 3
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_LINE = re.compile(r"Encountered an error|\b[A-Z]\w+ Error\b")


def _run_parsed(dbt: Dbt, project: Path, *args: str) -> tuple[int, str, dict | None, int]:
    """Run dbt and read the manifest it wrote. When dbt could not even parse the project
    (no manifest), run it again, up to PARSE_TRIES times: one data-eng-bench base failed
    to parse in 1 of 71 snapshots and parsed in every other one (decision 0004). A real
    compile error fails every try. Returns the exit code, output, manifest and tries."""
    manifest_file = project / "target" / "manifest.json"
    code, output = 1, ""
    for tries in range(1, PARSE_TRIES + 1):
        manifest_file.unlink(missing_ok=True)
        code, output = dbt.run(project, *args)
        manifest = read_json(manifest_file)
        if manifest is not None:
            return code, output, manifest, tries
    return code, output, None, PARSE_TRIES


def dbt_error(output: str, limit: int = 1200) -> str:
    """What dbt said went wrong: the lines from its first error on, not the end of its
    output, which is often only dbt's summary of deprecations."""
    lines = [_ANSI.sub("", line).rstrip() for line in output.strip().splitlines()]
    start = next((i for i, line in enumerate(lines) if _ERROR_LINE.search(line)), None)
    picked = lines[start : start + 15] if start is not None else lines[-15:]
    return "\n".join(picked)[:limit]


def _needs_deps(project: Path) -> bool:
    has_packages = (project / "packages.yml").is_file() or (project / "dependencies.yml").is_file()
    return has_packages and not (project / "dbt_packages").is_dir()


def profile_database(project_dir: Path, profile: Profile) -> Path:
    """The DuckDB file a profile points at, relative paths taken from the project."""
    path = str(profile.output.get("path", ""))
    if not path or "{{" in path or path == ":memory:":
        raise VerifierError("cannot tell the project's DuckDB file from its profile (pass --db)")
    candidate = Path(path)
    return (candidate if candidate.is_absolute() else project_dir / candidate).resolve()


def snapshot(
    project_dir: Path,
    db_path: Path | None,
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
    if db_path is None:
        db_path = profile_database(project_dir, profile)
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
    code, output, manifest, parse_tries = _run_parsed(dbt, project, "build")
    statuses = node_statuses(project)
    # A project that does not parse on any try is a real failed run (a compile error can
    # be the bug). Then there is no base manifest and no base build: the checks fall back
    # to the base's source files, and collateral has nothing to compare against.
    state["base_parsed"] = manifest is not None
    if manifest is not None:
        (work / "base" / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    state["statuses"] = statuses
    state["stats"] = (
        table_stats(project.parent / "db" / state["db_name"], manifest, statuses)
        if manifest is not None
        else {}
    )
    # Calibration (decision 0004, with Aditya): build the unchanged base a second time on a
    # fresh copy. A model whose contents differ between two identical builds is unstable
    # (timestamps, random values); collateral compares only its row count, or nothing if
    # even that moves.
    state["unstable"] = {}
    if manifest is not None:
        again, dbt_again = _build_copy(work, "base-build-2", state, dbt_exe, timeout)
        _run_parsed(dbt_again, again, "build")
        second = table_stats(again.parent / "db" / state["db_name"], manifest, node_statuses(again))
        state["unstable"] = checks.unstable_models(state["stats"], second)
        shutil.rmtree(work / "base-build-2", ignore_errors=True)
    (work / "state.json").write_text(json.dumps(state, indent=1), "utf-8")
    failures = sorted(uid for uid, s in statuses.items() if s["status"] in FAILED_STATUSES)
    return {
        "ok": True,
        "base_parsed": state["base_parsed"],
        "base_parse_error": None if manifest is not None else dbt_error(output),
        "base_parse_tries": parse_tries,
        "base_failures": failures,
        "unstable_models": len(state["unstable"]),
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


def verify(
    work: Path,
    diff_text: str,
    *,
    dbt_exe: str,
    timeout: float = 1500.0,
    replay: bool = False,
    policy: Policy = FIX,
) -> dict:
    """The verifier's decision on a diff. `replay` is for re-verifying recorded diffs
    (bench/replay.py), never a real run: each diff starts from the snapshot as it was
    (nothing learned is written back), a collateral failure does not stop row accounting,
    so both are measured, and the build copy is removed afterwards. `policy` is `fix` for
    an agent's fix (every benchmark number) or `pr` for any pull request (policy.py)."""
    try:
        return _verify(
            work, diff_text, dbt_exe=dbt_exe, timeout=timeout, replay=replay, policy=policy
        )
    finally:
        if replay:
            shutil.rmtree(work / "runs" / "replay", ignore_errors=True)


def _verify(
    work: Path, diff_text: str, *, dbt_exe: str, timeout: float, replay: bool, policy: Policy
) -> dict:
    started = time.monotonic()
    state = read_json(work / "state.json")
    base_manifest = read_json(work / "base" / "manifest.json")
    if state is None or (base_manifest is None and state.get("base_parsed", True)):
        raise VerifierError(f"no snapshot in {work} (run snapshot first)")
    base_project = work / "base" / "project"

    if not diff_text.strip():
        if policy.is_pr:
            nothing = checks.Check("empty_diff", True, "No change to the dbt project to check.")
            return _result([nothing], started)
        empty = checks.Check("empty_diff", False, "The diff changes nothing.")
        return _result([empty], started)
    try:
        patches: list[FilePatch] = parse_diff(diff_text)
    except DiffError as exc:
        return _result([checks.Check("diff", False, f"The diff cannot be read: {exc}")], started)
    if not patches:
        return _result([checks.Check("diff", False, "The diff names no files.")], started)

    found = [checks.tamper_paths(patches, state["paths"], policy)]
    if not found[-1].passed:
        return _result(found, started)
    if policy.is_pr and not checks.touches_dbt(patches, state["paths"]):
        return _result(found, started)  # nothing in the dbt project changed

    runs = work / "runs"
    number = len(list(runs.iterdir())) + 1 if runs.is_dir() else 1
    name = "runs/replay" if replay else f"runs/{number}"
    project, dbt = _build_copy(work, name, state, dbt_exe, timeout)
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

    code, output, new_manifest, _ = _run_parsed(dbt, project, "parse")
    if code != 0 or new_manifest is None:
        found.append(checks.Check("clean_rebuild", False, f"dbt parse failed: {dbt_error(output)}"))
        return _result(found, started)

    # Tamper has two halves, the paths and the tests; report them as one check.
    if base_manifest is not None:
        tests = checks.tamper_tests(base_manifest, new_manifest, policy)
    else:
        tests = checks.tamper_tests_from_source(base_project, project, state["paths"], policy)
    found[0] = checks.Check("tamper", tests.passed, f"{found[0].detail}; {tests.detail}")
    if not tests.passed:
        found[0].detail = tests.detail
        return _result(found, started)
    if base_manifest is not None:
        found.append(
            checks.masking(
                base_manifest, new_manifest, patches, policy, state.get("statuses") or {}
            )
        )
    else:
        found.append(
            checks.masking_from_source(base_project, project, new_manifest, patches, state["paths"])
        )
    if not found[-1].passed:
        return _result(found, started)

    vars_changed = project_vars(project) != state.get("vars")
    if base_manifest is not None:
        changed = checks.changed_nodes(base_manifest, new_manifest, vars_changed)
        downstream = checks.downstream_of(changed, new_manifest)
        base_statuses = state.get("statuses") or {}
    else:  # no base build: nothing excuses a failure
        changed, downstream, base_statuses = set(), None, None

    code, output = dbt.run(project, "build")
    statuses = node_statuses(project)
    rebuild = checks.clean_rebuild(code, statuses, output, base_statuses, downstream)
    skipped = checks.skipped_behind_old_failures(statuses, base_statuses, downstream)
    if not rebuild.passed and skipped:
        # Nodes the fix must pass were skipped only because something upstream already
        # failed before the change. Build them on their own, against the tables the full
        # build left, and judge that (decision 0004, with Aditya).
        names = checks.selectable_names(skipped, new_manifest)
        code, output = dbt.run(project, "build", "--select", *names)
        statuses = {**statuses, **node_statuses(project)}
        rebuild = checks.clean_rebuild(code, statuses, output, base_statuses, downstream)
        rebuild.detail = (
            f"{rebuild.detail}; {len(skipped)} node(s) skipped behind an old failure "
            "upstream were built on their own"
        )[:1500]
    found.append(rebuild)
    if not found[-1].passed:
        return _result(found, started)

    if base_manifest is None:
        found.append(checks.Check("collateral", True, checks.NO_BASE_BUILD))
        found.append(_row_accounting(work, state, None, new_manifest, project, patches, statuses))
        return _result(found, started)
    built_manifest = read_json(project / "target" / "manifest.json") or new_manifest
    changed = checks.changed_nodes(base_manifest, built_manifest, vars_changed)
    in_lineage = checks.lineage(changed, base_manifest, built_manifest)
    new_stats = table_stats(project.parent / "db" / state["db_name"], built_manifest, statuses)
    unstable = state.get("unstable") or {}
    collateral = checks.collateral(state["stats"], new_stats, in_lineage, unstable)
    suspects = checks.collateral_suspects(state["stats"], new_stats, in_lineage, unstable)
    if not collateral.passed and suspects:
        # Recheck before rejecting (decision 0004, with Aditya): two calibration builds
        # often agree by chance on a model that only sometimes differs (the stability probe,
        # bench/results/probe-stability-2026-11-20). Rebuild just the suspects and their
        # parents on the unchanged base, a few times; whatever moves there is unstable.
        moved, builds = _recheck(work, state, base_manifest, suspects, dbt_exe, timeout)
        newly = {
            uid: kind for uid, kind in moved.items() if unstable.get(uid) not in ("rows", kind)
        }
        if newly:
            unstable = {**unstable, **newly}
            state["unstable"] = unstable
            if not replay:
                (work / "state.json").write_text(json.dumps(state, indent=1), "utf-8")
            collateral = checks.collateral(state["stats"], new_stats, in_lineage, unstable)
        found_text = (
            f"{len(newly)} moved there too: {', '.join(sorted(newly)[:5])}"
            if newly
            else "none moved there"
        )
        collateral.detail = (
            f"{collateral.detail}; {len(suspects)} suspect model(s) were rebuilt on the "
            f"unchanged base ({builds} build(s)), and {found_text}"
        )[:1500]
    found.append(collateral)
    if not collateral.passed and not replay:
        return _result(found, started)
    found.append(
        _row_accounting(work, state, base_manifest, built_manifest, project, patches, statuses)
    )
    return _result(found, started)


def _compiled_sql(node: dict[str, Any], project: Path) -> str | None:
    code = node.get("compiled_code")
    if code:
        return str(code)
    path = (
        project
        / "target"
        / "compiled"
        / str(node.get("package_name"))
        / str(node.get("original_file_path"))
    )
    return path.read_text("utf-8") if path.is_file() else None


def _row_accounting(
    work: Path,
    state: dict[str, Any],
    base_manifest: dict[str, Any] | None,
    built_manifest: dict[str, Any],
    project: Path,
    patches: list[FilePatch],
    statuses: dict[str, dict[str, str]],
) -> checks.Check:
    """Check 5 over every SQL model whose file the diff touched and that built."""
    import duckdb  # imported here so the rest of the package loads without it

    touched = {p.path.replace("\\", "/") for p in patches}
    base_nodes = (base_manifest or {}).get("nodes", {})
    base_project = work / "base-build" / "project"
    results: list[accounting.ModelResult] = []
    todo: list[tuple[str, str, str | None, str]] = []
    for uid, node in sorted(built_manifest.get("nodes", {}).items()):
        if node.get("resource_type") != "model":
            continue
        if str(node.get("original_file_path", "")).replace("\\", "/") not in touched:
            continue
        materialized = (node.get("config") or {}).get("materialized")
        skip = None
        if node.get("language", "sql") != "sql":
            skip = "a Python model"
        elif materialized in ("incremental", "ephemeral"):
            skip = f"{materialized}"
        elif statuses.get(uid, {}).get("status") != "success":
            skip = "it did not build"
        sql = None if skip else _compiled_sql(node, project)
        if skip is None and sql is None:
            skip = "no compiled SQL"
        if skip is not None or sql is None:
            results.append(accounting.ModelResult(uid, skipped=skip))
            continue
        base = base_nodes.get(uid)
        base_sql = _compiled_sql(base, base_project) if base else None
        relation = f"{_quote(node['schema'])}.{_quote(node.get('alias') or node['name'])}"
        todo.append((uid, sql, base_sql, relation))
    if todo:
        deadline = time.monotonic() + accounting.TOTAL_BUDGET_S
        con = duckdb.connect(str(project.parent / "db" / state["db_name"]), read_only=True)
        try:
            for uid, sql, base_sql, relation in todo:
                results.append(accounting.analyse(con, uid, sql, base_sql, relation, deadline))
        finally:
            con.close()
    return checks.row_accounting(results)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


RECHECK_BUILDS = 5


def _recheck(
    work: Path,
    state: dict[str, Any],
    base_manifest: dict[str, Any],
    suspects: dict[str, str],
    dbt_exe: str,
    timeout: float,
) -> tuple[dict[str, str], int]:
    """Build the suspect models and their parents (no tests) on a fresh copy of the
    unchanged base, up to RECHECK_BUILDS times, stopping once every suspect has moved as
    much as it did after the fix. Returns how each suspect moved, and the builds run."""
    project, dbt = _build_copy(work, "recheck", state, dbt_exe, timeout)
    nodes = base_manifest.get("nodes", {})
    subset = {"nodes": {uid: nodes[uid] for uid in suspects if uid in nodes}}
    selection = [f"+{name}" for name in checks.selectable_names(sorted(suspects), base_manifest)]
    before = {uid: state["stats"][uid] for uid in suspects}
    db = project.parent / "db" / state["db_name"]
    moved: dict[str, str] = {}
    builds = 0
    try:
        while builds < RECHECK_BUILDS:
            builds += 1
            _run_parsed(
                dbt, project, "build", "--select", *selection, "--exclude", "resource_type:test"
            )
            after = table_stats(db, subset, node_statuses(project))
            for uid, kind in checks.unstable_models(before, after).items():
                if moved.get(uid) != "rows":
                    moved[uid] = kind
            if all(checks.excused(moved.get(uid), need) for uid, need in suspects.items()):
                break
    finally:
        shutil.rmtree(work / "recheck", ignore_errors=True)
    return moved, builds

"""The four checks of SPEC section 5, as plain functions over manifests, diffs and build
results. Each returns a Check; the first failure stops the run (see verify.py)."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from verifier.patch import FilePatch
from verifier.project import BUILT_TYPES, FAILED_STATUSES

MAX_LISTED = 8
ALWAYS_ALLOWED_FILES = frozenset({"dbt_project.yml", "packages.yml"})
NEVER_ALLOWED_DIRS = ("verifier", "bench", ".github", ".gitlab", ".circleci")

# Test settings that make a test weaker when changed.
TEST_CONFIG_KEYS = (
    "severity",
    "where",
    "limit",
    "error_if",
    "warn_if",
    "fail_calc",
    "enabled",
    "tags",
)

# An added line in a SQL file that makes a model return nothing.
MASKING_PATTERNS = (
    (re.compile(r"\blimit\s+0\b", re.I), "limit 0"),
    (re.compile(r"\b(where|on|having)\s+(false|0)\b", re.I), "an always-false filter"),
    (re.compile(r"\band\s+false\b", re.I), "an always-false filter"),
    (re.compile(r"\b(\d+)\s*=\s*(?!\1\b)\d+\b"), "an always-false comparison"),
    (re.compile(r"\b(\d+)\s*(<>|!=)\s*\1\b"), "an always-false comparison"),
    (re.compile(r"'([^']*)'\s*=\s*'(?!\1')[^']*'"), "an always-false comparison"),
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _listed(items: list[str]) -> str:
    shown = "; ".join(items[:MAX_LISTED])
    more = len(items) - MAX_LISTED
    return shown + (f"; and {more} more" if more > 0 else "")


# ---- 1. Tamper ----------------------------------------------------------------------


def tamper_paths(patches: list[FilePatch], paths: dict[str, list[str]]) -> Check:
    """Every changed file is a model, macro, seed, snapshot or analysis, or the project
    or package config. Tests, CI, the verifier and the benchmark are never allowed."""
    editable = [
        folder
        for key in ("model-paths", "macro-paths", "seed-paths", "snapshot-paths", "analysis-paths")
        for folder in paths[key]
    ]
    test_dirs = paths["test-paths"]
    refused: list[str] = []
    for patch in patches:
        for path in {p for p in (patch.old_path, patch.new_path) if p}:
            first = path.split("/", 1)[0]
            name = path.rsplit("/", 1)[-1]
            if first in NEVER_ALLOWED_DIRS or first in test_dirs:
                refused.append(f"{path} is off-limits")
            elif name == "profiles.yml":
                refused.append(f"{path} is a connection profile")
            elif path in ALWAYS_ALLOWED_FILES:
                continue
            elif not any(path.startswith(folder + "/") for folder in editable):
                refused.append(f"{path} is not a model, macro, seed, snapshot or analysis")
    if refused:
        return Check("tamper", False, "Changed files that a fix may not touch: " + _listed(refused))
    return Check("tamper", True, f"{len(patches)} changed file(s), all allowed")


def _test_signature(node: dict[str, Any]) -> dict[str, Any]:
    config = node.get("config") or {}
    signature: dict[str, Any] = {key: config.get(key) for key in TEST_CONFIG_KEYS}
    signature["node_tags"] = sorted(node.get("tags") or [])
    signature["test_metadata"] = node.get("test_metadata")
    signature["checksum"] = (node.get("checksum") or {}).get("checksum")
    return signature


def _changed_macros(base: dict[str, Any], new: dict[str, Any]) -> list[str]:
    before = base.get("macros", {})
    changed = []
    for uid, macro in new.get("macros", {}).items():
        old = before.get(uid)
        if old is None or old.get("macro_sql") != macro.get("macro_sql"):
            changed.append(uid)
    return changed


def _is_test_macro(uid: str) -> bool:
    name = uid.rsplit(".", 1)[-1]
    return name.startswith("test_") or "__test_" in name


def tamper_tests(base: dict[str, Any], new: dict[str, Any]) -> Check:
    """No test removed, disabled, loosened or re-tagged, and no test macro changed."""
    problems: list[str] = []
    new_nodes = new.get("nodes", {})
    for uid, node in base.get("nodes", {}).items():
        if node.get("resource_type") != "test":
            continue
        after = new_nodes.get(uid)
        if after is None:
            problems.append(f"{uid} was removed or disabled")
            continue
        before_sig, after_sig = _test_signature(node), _test_signature(after)
        changed = [key for key in before_sig if before_sig[key] != after_sig[key]]
        if changed:
            problems.append(f"{uid} changed: {', '.join(changed)}")
    new_units = new.get("unit_tests", {})
    for uid, unit in base.get("unit_tests", {}).items():
        after = new_units.get(uid)
        if after is None:
            problems.append(f"{uid} was removed or disabled")
        elif any(unit.get(k) != after.get(k) for k in ("given", "expect", "overrides")):
            problems.append(f"{uid} changed")
    for uid in _changed_macros(base, new):
        if _is_test_macro(uid):
            problems.append(f"test macro {uid} was added or changed")
    if problems:
        return Check("tamper", False, "Tests were weakened: " + _listed(problems))
    tests = sum(1 for n in base.get("nodes", {}).values() if n.get("resource_type") == "test")
    return Check("tamper", True, f"all {tests} test(s) unchanged")


# ---- 2. Masking ---------------------------------------------------------------------


def _filtered_lines(patches: list[FilePatch]) -> list[str]:
    problems: list[str] = []
    for patch in patches:
        if not patch.path.endswith(".sql"):
            continue
        for line in patch.added_lines():
            code = line.split("--", 1)[0]
            for pattern, what in MASKING_PATTERNS:
                if pattern.search(code):
                    problems.append(f"{patch.path} adds {what}: {code.strip()[:120]}")
                    break
    return problems


def masking(base: dict[str, Any], new: dict[str, Any], patches: list[FilePatch]) -> Check:
    problems: list[str] = []
    enabled_names = {
        (node.get("resource_type"), node.get("name"))
        for node in new.get("nodes", {}).values()
        if node.get("resource_type") in BUILT_TYPES
    }
    new_nodes = new.get("nodes", {})
    for uid, node in base.get("nodes", {}).items():
        if node.get("resource_type") not in BUILT_TYPES or uid in new_nodes:
            continue
        if (node.get("resource_type"), node.get("name")) in enabled_names:
            continue  # replaced by an enabled node of the same name (a package override)
        problems.append(f"{uid} was disabled or removed")
    problems += _filtered_lines(patches)
    if problems:
        return Check("masking", False, "The fix hides the failure: " + _listed(problems))
    return Check("masking", True, "no model disabled, no rows filtered away")


# ---- 3. Clean rebuild ---------------------------------------------------------------


def clean_rebuild(
    code: int,
    statuses: dict[str, dict[str, str]],
    output: str,
    base_statuses: dict[str, dict[str, str]] | None = None,
    downstream: set[str] | None = None,
) -> Check:
    """SPEC section 5: the build passes for the failed node and everything downstream.

    With a base build (decision 0004, with Aditya): the changed nodes and everything
    downstream of them must pass, and nothing that passed before may fail now. A node that
    already failed before the change and is not downstream of it may still fail: some
    projects never build cleanly (data-eng-bench fails 293 nodes before any change). Those
    are listed. Without a base build every node must pass.
    """
    failing, excused = _sort_failures(statuses, base_statuses, downstream)
    failed = [
        f"{uid} {statuses[uid]['status']}: {statuses[uid]['message']}".rstrip(": ")
        for uid in failing
    ]
    if failed:
        return Check("clean_rebuild", False, "dbt build failed: " + _listed(failed))
    if not statuses or (code != 0 and not excused):
        tail = "\n".join(output.strip().splitlines()[-15:])
        return Check("clean_rebuild", False, f"dbt build exited {code}: {tail}"[:1500])
    detail = f"dbt build passed: {len(statuses) - len(excused)} node(s)"
    if excused:
        detail += (
            f"; {len(excused)} node(s) that already failed before the change, none downstream "
            f"of it, still fail: {_listed(sorted(excused))}"
        )
    return Check("clean_rebuild", True, detail[:1500])


def _sort_failures(
    statuses: dict[str, dict[str, str]],
    base_statuses: dict[str, dict[str, str]] | None,
    downstream: set[str] | None,
) -> tuple[list[str], list[str]]:
    """(failures that count, old failures outside the fix that are excused)."""
    must_pass = downstream or set()
    failing: list[str] = []
    excused: list[str] = []
    for uid, info in statuses.items():
        if info["status"] not in FAILED_STATUSES:
            continue
        before = (base_statuses or {}).get(uid, {}).get("status")
        if base_statuses is not None and before in FAILED_STATUSES and uid not in must_pass:
            excused.append(uid)
        else:
            failing.append(uid)
    return failing, excused


def skipped_behind_old_failures(
    statuses: dict[str, dict[str, str]],
    base_statuses: dict[str, dict[str, str]] | None,
    downstream: set[str] | None,
) -> list[str]:
    """Nodes the fix must pass that the full build skipped, when skips are the only
    failures that count: something upstream that already failed blocked them, so the
    full build never judged the fix. Empty when anything really failed."""
    if base_statuses is None:
        return []
    failing, _ = _sort_failures(statuses, base_statuses, downstream)
    if failing and all(statuses[uid]["status"] == "skipped" for uid in failing):
        return sorted(failing)
    return []


def selectable_names(uids: list[str], manifest: dict[str, Any]) -> list[str]:
    """dbt --select names for these nodes: models, seeds and snapshots by name, and a
    test by the nodes it tests (dbt build runs a selected node's tests with it)."""
    nodes = manifest.get("nodes", {})
    names: set[str] = set()
    for uid in uids:
        node = nodes.get(uid) or {}
        if node.get("resource_type") in BUILT_TYPES:
            names.add(str(node["name"]))
        elif node.get("resource_type") == "test":
            for parent in (node.get("depends_on") or {}).get("nodes") or []:
                parent_node = nodes.get(parent) or {}
                if parent_node.get("resource_type") in BUILT_TYPES:
                    names.add(str(parent_node["name"]))
    return sorted(names)


def downstream_of(changed: set[str], manifest: dict[str, Any]) -> set[str]:
    """The changed nodes and everything downstream of them, tests included."""
    return _reach(changed, manifest.get("child_map", {}))


# ---- 4. No collateral damage --------------------------------------------------------


def changed_nodes(
    base: dict[str, Any], new: dict[str, Any], vars_changed: bool = False
) -> set[str]:
    """Nodes whose code or config changed, new and removed nodes, and nodes that use a
    changed macro. A change to the project's vars can change any model."""
    base_nodes, new_nodes = base.get("nodes", {}), new.get("nodes", {})
    if vars_changed:
        return set(base_nodes) | set(new_nodes)
    macros = set(_changed_macros(base, new))
    changed = set(base_nodes) ^ set(new_nodes)
    for uid, node in new_nodes.items():
        old = base_nodes.get(uid)
        if old is None:
            continue
        if (
            (old.get("checksum") or {}).get("checksum")
            != (node.get("checksum") or {}).get("checksum")
            or old.get("config") != node.get("config")
            or macros & set((node.get("depends_on") or {}).get("macros") or [])
        ):
            changed.add(uid)
    return changed


def _reach(start: set[str], edges: dict[str, list[str]]) -> set[str]:
    seen = set(start)
    queue = deque(start)
    while queue:
        for nxt in edges.get(queue.popleft(), []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def lineage(changed: set[str], base: dict[str, Any], new: dict[str, Any]) -> set[str]:
    reached: set[str] = set()
    for manifest in (base, new):
        reached |= _reach(changed, manifest.get("parent_map", {}))
        reached |= _reach(changed, manifest.get("child_map", {}))
    return reached


def unstable_models(first: dict[str, Any], second: dict[str, Any]) -> dict[str, str]:
    """Models that differ between two builds of the same unchanged project: "contents"
    when only the checksum moved, "rows" when even the row count did."""
    unstable: dict[str, str] = {}
    for uid, a in first.items():
        b = second.get(uid)
        if a is None or b is None:
            continue
        if a[0] != b[0]:
            unstable[uid] = "rows"
        elif a[1] != b[1]:
            unstable[uid] = "contents"
    return unstable


def collateral(
    base_stats: dict[str, Any],
    new_stats: dict[str, Any],
    in_lineage: set[str],
    unstable: dict[str, str] | None = None,
) -> Check:
    """Models outside the fix's lineage keep their row count and checksum. A model that
    is unstable between builds of the unchanged project is compared by row count only, or
    not at all if its row count moves too; the detail says how many."""
    unstable = unstable or {}
    compared = by_count = skipped = 0
    problems: list[str] = []
    for uid, before in sorted(base_stats.items()):
        if uid in in_lineage or before is None:
            continue
        if unstable.get(uid) == "rows":
            skipped += 1
            continue
        compared += 1
        after = new_stats.get(uid)
        if after is None:
            problems.append(f"{uid} could not be read after the fix")
        elif after[0] != before[0]:
            problems.append(f"{uid} rows {before[0]} -> {after[0]}")
        elif unstable.get(uid) == "contents":
            by_count += 1
        elif after[1] != before[1]:
            problems.append(f"{uid} same row count, different contents")
    if problems:
        return Check(
            "collateral",
            False,
            "Models outside the fix's lineage changed: " + _listed(problems),
        )
    detail = f"{compared} model(s) outside the lineage unchanged"
    if by_count or skipped:
        detail += (
            f" ({by_count} compared by row count only and {skipped} not compared, because "
            "they differ between builds of the unchanged project)"
        )
    return Check("collateral", True, detail)


def collateral_suspects(
    base_stats: dict[str, Any],
    new_stats: dict[str, Any],
    in_lineage: set[str],
    unstable: dict[str, str] | None = None,
) -> dict[str, str]:
    """The models collateral would reject that a rebuild of the unchanged base could
    excuse, with what they must be seen to do there: "rows" when their row count changed
    (only a model whose row count moves on its own is excused), "contents" when only the
    checksum did. A model that could not be read after the fix is never a suspect."""
    unstable = unstable or {}
    suspects: dict[str, str] = {}
    for uid, before in base_stats.items():
        if uid in in_lineage or before is None or unstable.get(uid) == "rows":
            continue
        after = new_stats.get(uid)
        if after is None:
            continue
        if after[0] != before[0]:
            suspects[uid] = "rows"
        elif after[1] != before[1] and unstable.get(uid) != "contents":
            suspects[uid] = "contents"
    return suspects


def excused(moved: str | None, needed: str) -> bool:
    """Whether a model seen to move like `moved` on the unchanged base excuses a change
    of kind `needed` after the fix."""
    return moved == "rows" or (moved == "contents" and needed == "contents")


# ---- When the base does not parse --------------------------------------------------
# A compile error can be the very bug, so the base may have no manifest. Tamper and
# masking then read the base's source files; collateral has no base build to compare.

NO_BASE_BUILD = "the base project did not parse, so there is no earlier build to compare"
TEST_KEYS = ("data_tests", "tests")
RESOURCE_KEYS = ("models", "sources", "seeds", "snapshots")
_TEST_DEFINITION = re.compile(r"{%-?\s*test\s+\w+|{%-?\s*macro\s+(\w+__)?test_\w+", re.I)
_SNAPSHOT_NAME = re.compile(r"{%-?\s*snapshot\s+(\w+)", re.I)


def _files(project: Path, folders: list[str], suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for folder in folders:
        root = project / folder
        if root.is_dir():
            found += sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in suffixes)
    return found


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _collect_tests(found: set[tuple[str, str]], owner: str, entry: Any) -> None:
    if not isinstance(entry, dict):
        return
    for key in TEST_KEYS:
        for test in entry.get(key) or []:
            found.add((owner, json.dumps(test, sort_keys=True, default=str)))
    for column in entry.get("columns") or []:
        if isinstance(column, dict):
            name = f"{owner}.{column.get('name')}"
            _collect_tests(found, name, {k: column.get(k) for k in TEST_KEYS})


def source_tests(project: Path, paths: dict[str, list[str]]) -> set[tuple[str, str]]:
    """Every test in the project's properties files, as (what it is on, the test)."""
    found: set[tuple[str, str]] = set()
    folders = paths["model-paths"] + paths["seed-paths"] + paths["snapshot-paths"]
    for path in _files(project, folders, (".yml", ".yaml")):
        try:
            document = yaml.safe_load(_read(path))
        except yaml.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        for kind in RESOURCE_KEYS:
            for item in document.get(kind) or []:
                if isinstance(item, dict):
                    owner = f"{kind}:{item.get('name')}"
                    _collect_tests(found, owner, item)
                    for table in item.get("tables") or []:
                        if isinstance(table, dict):
                            _collect_tests(found, f"{owner}.{table.get('name')}", table)
    return found


def _test_macros(project: Path, paths: dict[str, list[str]]) -> dict[str, str]:
    macros: dict[str, str] = {}
    for path in _files(project, paths["macro-paths"] + paths["test-paths"], (".sql",)):
        text = _read(path)
        if _TEST_DEFINITION.search(text):
            macros[path.relative_to(project).as_posix()] = text
    return macros


def tamper_tests_from_source(base: Path, new: Path, paths: dict[str, list[str]]) -> Check:
    problems = [
        f"{owner}: a test was removed or changed"
        for owner, _ in sorted(source_tests(base, paths) - source_tests(new, paths))
    ]
    before, after = _test_macros(base, paths), _test_macros(new, paths)
    problems += [
        f"test macro {rel} was added or changed"
        for rel in sorted(set(before) | set(after))
        if before.get(rel) != after.get(rel)
    ]
    if problems:
        return Check("tamper", False, "Tests were weakened: " + _listed(problems))
    return Check("tamper", True, "all tests in the base's properties files unchanged")


def source_nodes(project: Path, paths: dict[str, list[str]]) -> set[tuple[str, str]]:
    """(resource type, name) of every model, seed and snapshot in the project's files."""
    nodes = {("model", p.stem) for p in _files(project, paths["model-paths"], (".sql",))}
    nodes |= {("seed", p.stem) for p in _files(project, paths["seed-paths"], (".csv",))}
    for path in _files(project, paths["snapshot-paths"], (".sql",)):
        nodes |= {("snapshot", name) for name in _SNAPSHOT_NAME.findall(_read(path))}
    return nodes


def masking_from_source(
    base: Path, new: dict[str, Any], patches: list[FilePatch], paths: dict[str, list[str]]
) -> Check:
    enabled = {
        (node.get("resource_type"), node.get("name"))
        for node in new.get("nodes", {}).values()
        if node.get("resource_type") in BUILT_TYPES
    }
    problems = [
        f"{kind} {name} was disabled or removed"
        for kind, name in sorted(source_nodes(base, paths) - enabled)
    ]
    problems += _filtered_lines(patches)
    if problems:
        return Check("masking", False, "The fix hides the failure: " + _listed(problems))
    return Check("masking", True, "no model disabled, no rows filtered away")

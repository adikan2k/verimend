"""The four checks of SPEC section 5, as plain functions over manifests, diffs and build
results. Each returns a Check; the first failure stops the run (see verify.py)."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any

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
    for patch in patches:
        if not patch.path.endswith(".sql"):
            continue
        for line in patch.added_lines():
            code = line.split("--", 1)[0]
            for pattern, what in MASKING_PATTERNS:
                if pattern.search(code):
                    problems.append(f"{patch.path} adds {what}: {code.strip()[:120]}")
                    break
    if problems:
        return Check("masking", False, "The fix hides the failure: " + _listed(problems))
    return Check("masking", True, "no model disabled, no rows filtered away")


# ---- 3. Clean rebuild ---------------------------------------------------------------


def clean_rebuild(code: int, statuses: dict[str, dict[str, str]], output: str) -> Check:
    failed = [
        f"{uid} {info['status']}: {info['message']}".rstrip(": ")
        for uid, info in statuses.items()
        if info["status"] in FAILED_STATUSES
    ]
    if failed:
        return Check("clean_rebuild", False, "dbt build failed: " + _listed(failed))
    if code != 0 or not statuses:
        tail = "\n".join(output.strip().splitlines()[-15:])
        return Check("clean_rebuild", False, f"dbt build exited {code}: {tail}"[:1500])
    return Check("clean_rebuild", True, f"dbt build passed: {len(statuses)} node(s)")


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


def collateral(
    base_stats: dict[str, Any], new_stats: dict[str, Any], in_lineage: set[str]
) -> Check:
    compared = 0
    problems: list[str] = []
    for uid, before in sorted(base_stats.items()):
        if uid in in_lineage or before is None:
            continue
        compared += 1
        after = new_stats.get(uid)
        if after is None:
            problems.append(f"{uid} could not be read after the fix")
        elif after[0] != before[0]:
            problems.append(f"{uid} rows {before[0]} -> {after[0]}")
        elif after[1] != before[1]:
            problems.append(f"{uid} same row count, different contents")
    if problems:
        return Check(
            "collateral",
            False,
            "Models outside the fix's lineage changed: " + _listed(problems),
        )
    return Check("collateral", True, f"{compared} model(s) outside the lineage unchanged")

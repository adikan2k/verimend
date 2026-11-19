"""Where tasks come from, which sets may be run from where, and staging copies to run.

Sets:
- `dev`: bench/dev/<source>/, written by scripts/split_bench.py. Claude Code may read it.
- `heldout`: bench/heldout/<source>/. Only Aditya runs it, never from inside Claude Code.
- `vendor`: the benchmark's own checkout in ~/.verimend/vendor/. Used before the split
  exists (the task check and the first evidence runs), refused once the split exists.

A batch always runs on a staged copy, because ADE-bench writes the tests it generates
back into the task folder and would otherwise change the committed tasks.
"""

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from verimend.config import REPO_ROOT

SetName = Literal["dev", "heldout", "vendor"]
SET_NAMES: tuple[SetName, ...] = ("dev", "heldout", "vendor")

# Written by split_bench.py. Its presence means the split exists.
SPLIT_MARKER = REPO_ROOT / "bench" / "split.json"


@dataclass(frozen=True)
class Source:
    name: str
    repo_url: str
    commit: str  # the upstream commit every run must use
    tasks_subdir: str
    task_file: str  # a folder is a task if it has this file
    runner: Literal["ade", "harbor"]
    agents: tuple[str, ...]  # agents wired up for this source


SOURCES: dict[str, Source] = {
    "ade-bench": Source(
        name="ade-bench",
        repo_url="https://github.com/dbt-labs/ade-bench",
        commit="c305600ebf17e7e7c4550bd401b6ddb530beb7c8",
        tasks_subdir="tasks",
        task_file="task.yaml",
        runner="ade",
        agents=("none", "reference", "baseline", "verimend"),
    ),
    "data-eng-bench": Source(
        name="data-eng-bench",
        repo_url="https://github.com/snowflake-labs/data-eng-bench",
        commit="74d4bcd4192e1959754f683841f9f602b8bc86e1",
        tasks_subdir="tasks",
        task_file="task.toml",
        runner="harbor",
        # The baseline and VeriMend on Harbor come in phase 3.
        agents=("none", "reference"),
    ),
}


class TaskSetError(RuntimeError):
    """A task set cannot be used the way it was asked for."""


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError:
        raise TaskSetError(f"unknown source {name!r}; known: {', '.join(SOURCES)}") from None


def vendor_dir(source: Source, home: Path) -> Path:
    return home / "vendor" / source.name


def require_set_allowed(set_name: str, *, env: Mapping[str, str], split_exists: bool) -> SetName:
    """Refuse the runs that would break the held-out rules (CLAUDE.md rule 2).

    `CLAUDECODE` is set in every shell Claude Code starts, so a held-out batch started
    from inside Claude Code is refused here, before any task is read.
    """
    if set_name not in SET_NAMES:
        raise TaskSetError(f"--set must be one of {', '.join(SET_NAMES)}, got {set_name!r}")
    if set_name == "heldout" and env.get("CLAUDECODE"):
        raise TaskSetError(
            "Held-out batches are run by Aditya, never from inside Claude Code "
            "(CLAUDECODE is set). Run this from your own terminal."
        )
    if set_name == "vendor" and split_exists:
        raise TaskSetError(
            "The split exists (bench/split.json), so tasks run from bench/dev or "
            "bench/heldout, never straight from the vendor checkout."
        )
    return set_name  # type: ignore[return-value]


def tasks_root(set_name: SetName, source: Source, home: Path, repo: Path = REPO_ROOT) -> Path:
    if set_name == "vendor":
        return vendor_dir(source, home) / source.tasks_subdir
    return repo / "bench" / set_name / source.name


def list_task_ids(root: Path, source: Source) -> list[str]:
    if not root.is_dir():
        raise TaskSetError(f"no task folder at {root}")
    return sorted(p.parent.name for p in root.glob(f"*/{source.task_file}"))


def select_tasks(available: list[str], requested: list[str] | None, limit: int) -> list[str]:
    """The requested tasks (or all available ones), sorted, cut to `limit`."""
    if requested:
        unknown = sorted(set(requested) - set(available))
        if unknown:
            raise TaskSetError(f"not in this task set: {', '.join(unknown)}")
        chosen = sorted(set(requested))
    else:
        chosen = sorted(available)
    if not chosen:
        raise TaskSetError("no tasks to run")
    return chosen[:limit]


def stage_tasks(root: Path, task_ids: list[str], dest: Path) -> Path:
    """Copy the chosen task folders into dest/tasks and return that folder."""
    target = dest / "tasks"
    if target.exists():
        raise TaskSetError(f"staging folder already exists: {target}")
    target.mkdir(parents=True)
    for task_id in task_ids:
        shutil.copytree(root / task_id, target / task_id)
    return target


def docker_path(path: Path) -> str:
    """The path Docker Desktop uses for a host folder, so bind mounts resolve the same in
    every container: C:\\Users\\x -> /run/desktop/mnt/host/c/Users/x. POSIX paths pass
    through unchanged."""
    text = str(path)
    if re.match(r"^[A-Za-z]:[\\/]", text):
        rest = text[3:].replace("\\", "/").rstrip("/")
        return f"/run/desktop/mnt/host/{text[0].lower()}/{rest}".rstrip("/")
    return path.as_posix()

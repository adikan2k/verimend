"""Count ADE-bench tasks by status, category and tag. Prints IDs and labels only.

    <ade-bench venv python> -I scripts/spikes/count_ade_tasks.py <path to ade-bench/tasks>

Deliberately prints no prompts, descriptions or solutions: the split into development and
held-out tasks does not exist yet, so task content must not be read in bulk.
"""

import sys
from collections import Counter
from pathlib import Path

import yaml  # available in ADE-bench's own environment

FIX_TAGS = {"debugging", "bug", "bugfix", "bug-fix", "fix", "broken", "error", "errors"}


def main(tasks_dir: Path) -> None:
    rows: list[tuple[str, str, str, list[str]]] = []
    for task_yaml in sorted(tasks_dir.glob("*/task.yaml")):
        data = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        tags = [str(tag).lower() for tag in (data.get("tags") or [])]
        rows.append(
            (
                str(data.get("task_id", task_yaml.parent.name)),
                str(data.get("status", "?")),
                str(data.get("category") or "-"),
                tags,
            )
        )

    print(f"tasks: {len(rows)}")
    print("status:", dict(Counter(status for _, status, _, _ in rows)))
    print("category:", dict(Counter(category for _, _, category, _ in rows)))
    print("tags:", dict(Counter(tag for *_, tags in rows for tag in tags).most_common()))

    ready = [row for row in rows if row[1] == "ready"]
    fix = [row for row in ready if FIX_TAGS & set(row[3])]
    print(f"\nready: {len(ready)}; ready and tagged as fix-style: {len(fix)}")
    for task_id, _, category, tags in fix:
        print(f"  {task_id:28} {category:20} {','.join(tags)}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

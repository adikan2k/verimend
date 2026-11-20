"""What an edit_model call would write, worked out without writing it.

edit_model uses this to make the change; the PreToolUse hook uses it to judge the change
before it happens. One function, so the guard sees exactly what the tool would write.
"""

from typing import Any


def planned_content(before: str | None, args: dict[str, Any]) -> tuple[str | None, str | None]:
    """(new content, None), or (None, why the call is malformed)."""
    content = args.get("content")
    old, new = args.get("old_text"), args.get("new_text")
    if content is not None and old is None:
        return str(content), None
    if old is not None and new is not None and content is None:
        if before is None:
            return None, "the file does not exist; pass the whole content instead."
        found = before.count(str(old))
        if found != 1:
            return None, f"old_text must appear exactly once; it appears {found} times."
        return before.replace(str(old), str(new), 1), None
    return None, "Pass either content, or old_text and new_text."

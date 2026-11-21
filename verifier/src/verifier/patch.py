"""Unified diffs, stdlib only: make one from two folders, read one, apply one.

The verifier computes the diff of an agent's work itself (`make_diff`) and applies a
human's diff the same way it applies its own (`apply_diff`), so both go through one
path. Accepts git-style `a/` and `b/` prefixes, new files and deleted files.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEV_NULL = "/dev/null"
NO_NEWLINE = "\\ No newline at end of file"

# Never part of a project's source: build output, installed tooling, databases.
SKIP_DIRS = frozenset({"target", "logs", ".git", "__pycache__", ".venv", "node_modules"})
SKIP_SUFFIXES = (".duckdb", ".duckdb.wal", ".pyc")
SKIP_FILES = frozenset({".user.yml", "package-lock.yml"})
# The connection, not the code: left out of a computed diff (decision 0004). The verifier
# never builds with the project's own profiles.yml; it writes one pointed at its copy.
ENVIRONMENT_FILES = frozenset({"profiles.yml"})

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def split_lines(text: str) -> list[str]:
    """Lines with their endings, split on newlines only (str.splitlines also splits on
    form feeds and other separators, which a diff does not)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


class DiffError(ValueError):
    """A diff that cannot be read, computed or applied."""


@dataclass
class Hunk:
    old_start: int
    old_count: int = 1
    lines: list[str] = field(default_factory=list)  # each starts with " ", "-" or "+"
    no_newline_old: bool = False
    no_newline_new: bool = False

    def old_lines(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    def new_lines(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]


@dataclass
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        path = self.new_path or self.old_path
        assert path is not None
        return path

    def added_lines(self) -> list[str]:
        return [line[1:] for hunk in self.hunks for line in hunk.lines if line[0] == "+"]


def project_files(root: Path) -> dict[str, Path]:
    """Relative POSIX path -> file, skipping build output and databases."""
    found: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_FILES or name in ENVIRONMENT_FILES or name.endswith(SKIP_SUFFIXES):
                continue
            path = Path(dirpath) / name
            found[path.relative_to(root).as_posix()] = path
    return found


def _read_text(path: Path, rel: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiffError(f"binary file changed: {rel}") from exc


def _diff_lines(lines: list[str]) -> list[str]:
    """difflib keeps each line's own ending; mark a last line that has none."""
    out: list[str] = []
    for line in lines:
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append(NO_NEWLINE + "\n")
    return out


def make_diff(base: Path, changed: Path) -> str:
    """The unified diff that turns the files under `base` into those under `changed`."""
    before, after = project_files(base), project_files(changed)
    parts: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel)
        new = after.get(rel)
        if old is not None and new is not None and old.read_bytes() == new.read_bytes():
            continue
        a = split_lines(_read_text(old, rel)) if old else []
        b = split_lines(_read_text(new, rel)) if new else []
        body = list(
            difflib.unified_diff(
                a,
                b,
                fromfile=f"a/{rel}" if old else DEV_NULL,
                tofile=f"b/{rel}" if new else DEV_NULL,
                n=3,
            )
        )
        if not body:
            continue
        header, hunks = body[:2], body[2:]
        parts.append(f"diff --git a/{rel} b/{rel}\n")
        parts.extend(line if line.endswith("\n") else line + "\n" for line in header)
        parts.extend(_diff_lines(hunks))
    return "".join(parts)


def _header_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == DEV_NULL:
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_diff(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    lines = split_lines(text)
    i = 0
    current: FilePatch | None = None
    hunk: Hunk | None = None
    old_left = new_left = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(_header_path(line[4:]), _header_path(lines[i + 1][4:]))
            if current.old_path is None and current.new_path is None:
                raise DiffError("a file header names /dev/null twice")
            patches.append(current)
            hunk = None
            i += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise DiffError(f"hunk before any file header: {line.strip()}")
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            hunk = Hunk(old_start=int(match.group(1)), old_count=old_left)
            current.hunks.append(hunk)
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            i += 1
            continue
        if hunk is not None and (old_left > 0 or new_left > 0) and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line if line.endswith("\n") else line + "\n")
            if line[0] in " -":
                old_left -= 1
            if line[0] in " +":
                new_left -= 1
            i += 1
            continue
        if hunk is not None and line.startswith("\\") and hunk.lines:
            # Belongs to the line just before it.
            last = hunk.lines[-1]
            hunk.lines[-1] = _strip_end(last)
            if last[0] in " -":
                hunk.no_newline_old = True
            if last[0] in " +":
                hunk.no_newline_new = True
            i += 1
            continue
        if hunk is not None and line.strip() == "" and (old_left > 0 or new_left > 0):
            # Some editors strip the single space of an empty context line.
            hunk.lines.append(" \n")
            old_left -= 1
            new_left -= 1
            i += 1
            continue
        # git's "diff --git", "index", "new file mode" and anything else between files.
        hunk = None
        i += 1
    for patch in patches:
        if not patch.hunks and patch.old_path is not None and patch.new_path is not None:
            raise DiffError(f"no changes for {patch.path}")
    return patches


def _strip_end(line: str) -> str:
    """A line without its ending, CRLF or LF: a diff saved on Windows still fits a file
    with Unix endings, and the other way round."""
    return line.rstrip("\r\n")


def _find(lines: list[str], block: list[str], expected: int) -> int:
    def matches(at: int) -> bool:
        if at < 0 or at + len(block) > len(lines):
            return False
        return all(_strip_end(lines[at + k]) == _strip_end(block[k]) for k in range(len(block)))

    if matches(expected):
        return expected
    hits = [at for at in range(len(lines) - len(block) + 1) if matches(at)]
    if len(hits) == 1:
        return hits[0]
    raise DiffError("context not found" if not hits else "context matches in several places")


def apply_file(original: str, patch: FilePatch) -> str:
    lines = split_lines(original)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    offset = 0
    for hunk in patch.hunks:
        old_block = hunk.old_lines()
        new_block = hunk.new_lines()
        # "-5,0" inserts after line 5; otherwise the hunk starts at line old_start.
        start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        expected = max(start, 0) + offset
        try:
            at = _find(lines, old_block, expected) if old_block else expected
        except DiffError as exc:
            raise DiffError(f"{patch.path}: hunk at line {hunk.old_start}: {exc}") from exc
        replacement = [_strip_end(line) + eol for line in new_block]
        lines[at : at + len(old_block)] = replacement
        offset += len(new_block) - len(old_block)
    text = "".join(lines)
    ends_without_newline = any(h.no_newline_new for h in patch.hunks[-1:])
    if ends_without_newline:
        text = _strip_end(text)
    return text


def _inside(root: Path, rel: str) -> Path:
    if rel.startswith("/") or ".." in Path(rel).parts or re.match(r"^[A-Za-z]:", rel):
        raise DiffError(f"path outside the project: {rel}")
    return root / rel


def apply_diff(root: Path, patches: list[FilePatch]) -> None:
    """Apply in place under `root`. Raises DiffError, leaving earlier files changed."""
    for patch in patches:
        if patch.new_path is None:
            assert patch.old_path is not None
            target = _inside(root, patch.old_path)
            if not target.is_file():
                raise DiffError(f"cannot delete a missing file: {patch.old_path}")
            target.unlink()
            continue
        target = _inside(root, patch.new_path)
        if patch.old_path is None:
            if target.exists():
                raise DiffError(f"new file already exists: {patch.new_path}")
            original = ""
        else:
            source = _inside(root, patch.old_path)
            if not source.is_file():
                raise DiffError(f"file to patch is missing: {patch.old_path}")
            original = source.read_bytes().decode("utf-8")
            if patch.old_path != patch.new_path:
                source.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(apply_file(original, patch).encode("utf-8"))

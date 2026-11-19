"""Run every check: lint, format, types and tests for Python, then the site once it exists.

    uv run python scripts/check.py

Runs all checks even if an early one fails, prints a summary and exits non-zero if any
check failed. Standard library only.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "site"


def python_checks() -> list[tuple[str, list[str]]]:
    py = sys.executable
    return [
        ("ruff check", [py, "-m", "ruff", "check", "."]),
        ("ruff format", [py, "-m", "ruff", "format", "--check", "."]),
        ("pyright", [py, "-m", "pyright"]),
        ("pytest", [py, "-m", "pytest"]),
    ]


def site_checks() -> list[tuple[str, list[str]]]:
    """Site checks, or an empty list until site/package.json exists (phase 7)."""
    if not (SITE / "package.json").is_file():
        return []
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        # A missing tool must fail the run, not silently skip the site.
        return [("site: pnpm not found", [sys.executable, "-c", "raise SystemExit(1)"])]
    return [
        (f"site {name}", [pnpm, "-C", str(SITE), name]) for name in ("lint", "typecheck", "test")
    ]


def run(checks: list[tuple[str, list[str]]]) -> list[tuple[str, bool, float]]:
    results: list[tuple[str, bool, float]] = []
    for name, command in checks:
        print(f"\n=== {name} ===", flush=True)
        started = time.monotonic()
        completed = subprocess.run(command, cwd=REPO, check=False)
        results.append((name, completed.returncode == 0, time.monotonic() - started))
    return results


def main() -> int:
    site = site_checks()
    results = run(python_checks() + site)

    print("\n=== summary ===")
    for name, passed, seconds in results:
        print(f"{'ok  ' if passed else 'FAIL'}  {name}  ({seconds:.1f}s)")
    if not site:
        print("skip  site checks (site/package.json does not exist yet)")

    failed = [name for name, passed, _ in results if not passed]
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

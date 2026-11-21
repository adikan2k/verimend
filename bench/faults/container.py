"""The plain-agent baseline on the fault suite, in a container of its own.

VeriMend's tools cannot leave the project, so it runs on this machine. The baseline is
Claude Code with a shell, so it runs in a container that holds nothing but the fault:

    /work/project   the built fault's project (a git repository), read-write
    /work/db        its database, read-write (dbt builds into it)
    /work/profiles  a profile pointing at /work/db, read-only

The faulted copy, the pristine database, the verifier's folder and bench/faults/ (checks
and references) are never mounted. The verifier snapshots on the host before the
container starts and checks on the host after it is gone, so it is out of the agent's
reach entirely. The command is the one the baseline runs on the public benchmarks
(bench.harbor.records.baseline_command), with the task prompt VeriMend gets and one more
line asking for the root-cause category.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from importlib.metadata import version
from pathlib import Path

from bench.agents import CLAUDE_CODE_VERSION
from bench.faults.dbt import PROFILE, TARGET
from bench.faults.demo import DB_NAME
from bench.faults.lib import CATEGORIES
from bench.harbor.records import baseline_command

IMAGE = "verimend-faults-baseline"
DOCKERFILE = Path(__file__).with_name("Dockerfile")
CONTAINER_TIMEOUT_SEC = 1200
CATEGORY_LINE = (
    "Near the end of your reply, on a line of its own, name the category of the root cause: "
    f"CATEGORY: followed by one of {', '.join(CATEGORIES)}."
)


class ContainerError(RuntimeError):
    """The baseline's container could not be built or run."""


def fault_prompt(task: str) -> str:
    """The task VeriMend gets, plus the category line. The verdict line is added by
    baseline_command, as on the public benchmarks."""
    return f"{task}\n\n{CATEGORY_LINE}"


def build_args() -> dict[str, str]:
    """The exact dbt and DuckDB versions of this environment, so the container reads the
    database the host built, and the pinned Claude Code CLI."""
    return {
        "DBT_CORE_VERSION": version("dbt-core"),
        "DBT_DUCKDB_VERSION": version("dbt-duckdb"),
        "DUCKDB_VERSION": version("duckdb"),
        "CLAUDE_CODE_VERSION": CLAUDE_CODE_VERSION,
    }


def ensure_image() -> None:
    """Build the image from the Dockerfile alone: no build context, so nothing of the
    repository (the faults' answers least of all) can end up in it."""
    command = ["docker", "build", "-q", "-t", IMAGE]
    for name, value in build_args().items():
        command += ["--build-arg", f"{name}={value}"]
    done = subprocess.run([*command, "-"], input=DOCKERFILE.read_bytes(), capture_output=True)
    if done.returncode != 0:
        raise ContainerError(f"docker build failed: {done.stderr.decode(errors='replace')[-800:]}")


def write_container_profile(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"{PROFILE}:\n  target: {TARGET}\n  outputs:\n    {TARGET}:\n"
        f"      type: duckdb\n      path: '/work/db/{DB_NAME}'\n      threads: 4\n"
    )
    (out_dir / "profiles.yml").write_text(text, "utf-8", newline="\n")
    return out_dir


def docker_argv(name: str, project: Path, db_dir: Path, profiles: Path, command: str) -> list[str]:
    """`docker run` for one trial. The API key is passed by name only: its value comes
    from the environment of the docker process, never the command line."""
    return [
        "docker", "run", "--rm", "--name", name,
        "-e", "ANTHROPIC_API_KEY",
        "-v", f"{project.resolve()}:/work/project",
        "-v", f"{db_dir.resolve()}:/work/db",
        "-v", f"{profiles.resolve()}:/work/profiles:ro",
        "-w", "/work/project",
        IMAGE, "bash", "-c", command,
    ]  # fmt: skip


def run_container(
    project: Path,
    db_dir: Path,
    profiles: Path,
    command: str,
    api_key: str | None,
    timeout: float = CONTAINER_TIMEOUT_SEC,
) -> tuple[str, float]:
    """Run `command` in a fresh container; return its output and how long it took."""
    name = f"verimend-fault-{uuid.uuid4().hex[:10]}"
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    started = time.monotonic()
    try:
        done = subprocess.run(docker_argv(name, project, db_dir, profiles, command), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)  # fmt: skip
        output = done.stdout + done.stderr
    except subprocess.TimeoutExpired as late:
        subprocess.run(["docker", "kill", name], capture_output=True)
        output = late.stdout if isinstance(late.stdout, str) else ""
    return output, round(time.monotonic() - started, 1)


def run_baseline(
    project: Path,
    db_dir: Path,
    work: Path,
    *,
    task: str,
    model: str,
    run_cap_usd: float,
    max_turns: int,
    api_key: str,
) -> tuple[str, float]:
    """The baseline on one built fault: its stream-json output and its seconds."""
    profiles = write_container_profile(work / "container-profiles")
    command = baseline_command(fault_prompt(task), model, run_cap_usd, max_turns)
    return run_container(project, db_dir, profiles, command, api_key)

"""action/verify/action.yml, the inspector for any pull request: no key, no model, inputs
reach scripts only through env, and it installs the dbt this repository is tested with."""

import re
from importlib.metadata import version
from pathlib import Path

import yaml

TEXT = (Path(__file__).parents[1] / "action" / "verify" / "action.yml").read_text()
ACTION = yaml.safe_load(TEXT)
STEPS = ACTION["runs"]["steps"]


def test_it_needs_no_api_key_and_calls_no_model() -> None:
    lowered = TEXT.lower()
    assert "anthropic" not in lowered and "secrets." not in lowered
    assert "verimend fix" not in lowered and "uv sync" not in lowered


def test_no_expression_is_spliced_into_a_script() -> None:
    # Pull request titles, labels and inputs reach a shell only as environment variables.
    for step in STEPS:
        assert "${{" not in step.get("run", ""), step["name"]


def test_every_input_it_reads_is_declared_and_every_output_has_a_step() -> None:
    used = set(re.findall(r"inputs\.([a-z-]+)", TEXT))
    assert used <= set(ACTION["inputs"]), used - set(ACTION["inputs"])
    ids = {s.get("id") for s in STEPS}
    for output in ACTION["outputs"].values():
        (step,) = re.findall(r"steps\.([a-z-]+)\.outputs", output["value"])
        assert step in ids


def test_it_installs_the_dbt_this_repository_is_tested_with() -> None:
    assert ACTION["inputs"]["dbt-version"]["default"] == version("dbt-core")
    assert ACTION["inputs"]["dbt-duckdb-version"]["default"] == version("dbt-duckdb")


def test_it_verifies_under_the_pr_policy_and_the_label_alone_allows_test_changes() -> None:
    verify = next(s for s in STEPS if s.get("id") == "verify")
    assert "--policy pr" in verify["run"]
    assert '[[ "$ALLOWED" == "true" ]] && check+=(--allow-test-changes)' in verify["run"]
    assert verify["env"]["ALLOWED"] == (
        "${{ contains(github.event.pull_request.labels.*.name, inputs.allow-label) }}"
    )


def test_a_failed_or_broken_check_fails_the_job() -> None:
    last = STEPS[-1]
    assert last["if"] == "always()"
    assert "passed)" in last["run"] and last["run"].count("exit 1") == 2


def test_only_first_party_actions_are_used() -> None:
    used = [s["uses"] for s in STEPS if "uses" in s]
    assert used and all(u.startswith("actions/") for u in used)

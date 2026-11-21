"""action/action.yml: the inputs SPEC section 10 names, the guard before anything runs,
and the API key reaching one step only."""

from pathlib import Path

import yaml

ACTION = yaml.safe_load((Path(__file__).parents[1] / "action" / "action.yml").read_text())
STEPS = ACTION["runs"]["steps"]


def test_inputs_are_the_key_the_project_the_cap_and_the_model() -> None:
    inputs = ACTION["inputs"]
    assert inputs["anthropic-api-key"]["required"] is True
    assert {"project-dir", "max-usd", "model", "max-attempts", "github-token"} <= set(inputs)
    assert inputs["max-usd"]["default"] == "0.25"


def test_the_guard_runs_first_and_every_later_step_depends_on_it() -> None:
    guard = STEPS[0]
    assert guard["id"] == "guard"
    assert "forks" in guard["run"] and "verimend/fix-*" in guard["run"]
    for step in STEPS[1:]:
        assert "steps.guard.outputs.skip != 'true'" in step["if"]


def test_the_api_key_reaches_only_the_step_that_runs_verimend() -> None:
    holders = [s for s in STEPS if "inputs.anthropic-api-key" in str(s)]
    assert [s.get("id") for s in holders] == ["run"]
    assert holders[0]["env"]["ANTHROPIC_API_KEY"] == "${{ inputs.anthropic-api-key }}"


def test_it_runs_verimend_fix_with_open_pr_from_its_own_lock_file() -> None:
    run = next(s for s in STEPS if s.get("id") == "run")["run"]
    assert "--open-pr" in run and "uv run --frozen --no-dev" in run
    install = next(s for s in STEPS if s.get("name") == "Install VeriMend")["run"]
    assert "uv sync --frozen --no-dev" in install


def test_only_first_party_actions_are_used() -> None:
    used = [s["uses"] for s in STEPS if "uses" in s]
    assert used and all(u.startswith("actions/") for u in used)

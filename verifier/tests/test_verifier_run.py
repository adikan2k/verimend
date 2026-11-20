"""The verifier end to end on a real dbt-duckdb project: snapshot once, then verify
hand-written diffs. The fixture's customer_totals reads `amout` instead of `amount`."""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from verifier.cli import main
from verifier.project import (
    Profile,
    VerifierError,
    default_dbt,
    read_profile,
    write_profiles,
)
from verifier.run import diff_from_project, snapshot, verify

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_shop"
DBT = default_dbt()

FIX = """\
--- a/models/marts/customer_totals.sql
+++ b/models/marts/customer_totals.sql
@@ -1,5 +1,5 @@
 -- Seeded bug for the tool tests: the column is amount, not amout.
 select
     customer_id,
-    sum(amout) as total_amount
+    sum(amount) as total_amount
 from {{ ref('stg_orders') }}
"""


@pytest.fixture(scope="module")
def snap(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict]:
    root = tmp_path_factory.mktemp("verifier")
    project = root / "project"
    shutil.copytree(FIXTURE, project)
    work = root / "work"
    taken = snapshot(project, project / "shop.duckdb", work, dbt_exe=DBT)
    return project, work, taken


def run(work: Path, diff: str) -> dict:
    return verify(work, diff, dbt_exe=DBT)


def test_snapshot_records_the_base_failure(snap):
    _, work, taken = snap
    assert taken["ok"] and taken["base_failures"] == ["model.tiny_shop.customer_totals"]
    state = json.loads((work / "state.json").read_text())
    assert state["stats"]["model.tiny_shop.order_count"][0] == 1


def test_the_real_fix_passes_every_check(snap):
    project, work, _ = snap
    result = run(work, FIX)
    assert result["passed"], result
    assert [c["name"] for c in result["checks"]] == [
        "tamper",
        "masking",
        "clean_rebuild",
        "collateral",
    ]
    # Nothing touched the real project or its database.
    assert not (project / "shop.duckdb").exists()
    assert "amout" in (project / "models/marts/customer_totals.sql").read_text()


def test_an_empty_diff_fails(snap):
    assert run(snap[1], "")["failed_check"] == "empty_diff"


def test_a_diff_that_does_not_apply_fails(snap):
    result = run(snap[1], FIX.replace("sum(amout)", "sum(nothing)"))
    assert result["failed_check"] == "diff"


def with_fix(extra: str) -> str:
    return FIX + extra


DELETE_TEST = """\
--- a/models/schema.yml
+++ b/models/schema.yml
@@ -5,4 +5,3 @@
       - name: order_id
         data_tests:
           - not_null
-          - unique
"""

WARN_TEST = """\
--- a/models/schema.yml
+++ b/models/schema.yml
@@ -5,4 +5,6 @@
       - name: order_id
         data_tests:
           - not_null
-          - unique
+          - unique:
+              config:
+                severity: warn
"""

EDIT_SINGULAR_TEST = """\
--- a/tests/assert_positive_amount.sql
+++ b/tests/assert_positive_amount.sql
@@ -1 +1 @@
-select * from {{ ref('stg_orders') }} where amount <= 0
+select * from {{ ref('stg_orders') }} where amount < -1000
"""

DISABLE_MODEL = """\
--- a/models/marts/customer_totals.sql
+++ b/models/marts/customer_totals.sql
@@ -1,2 +1,3 @@
+{{ config(enabled=false) }}
 -- Seeded bug for the tool tests: the column is amount, not amout.
 select
"""

LIMIT_ZERO = (
    FIX
    + """\
--- a/models/staging/stg_orders.sql
+++ b/models/staging/stg_orders.sql
@@ -3,3 +3,4 @@
     customer_id,
     amount
 from {{ ref('raw_orders') }}
+limit 0
"""
)

ALWAYS_FALSE = """\
--- a/models/marts/customer_totals.sql
+++ b/models/marts/customer_totals.sql
@@ -3,4 +3,5 @@
     customer_id,
-    sum(amout) as total_amount
+    sum(amount) as total_amount
 from {{ ref('stg_orders') }}
+where 1=0
 group by customer_id
"""

# Fixes the model, then empties a model outside its lineage after every run.
COLLATERAL = """\
--- a/dbt_project.yml
+++ b/dbt_project.yml
@@ -5,3 +5,4 @@
 model-paths: ["models"]
 seed-paths: ["seeds"]
 test-paths: ["tests"]
+on-run-end: "delete from main.order_count"
"""

STILL_BROKEN = """\
--- a/models/marts/customer_totals.sql
+++ b/models/marts/customer_totals.sql
@@ -3,4 +3,4 @@
     customer_id,
-    sum(amout) as total_amount
+    sum(amont) as total_amount
 from {{ ref('stg_orders') }}
 group by customer_id
"""


@pytest.mark.parametrize(
    ("name", "diff", "failed_check", "words"),
    [
        ("delete a test", with_fix(DELETE_TEST), "tamper", "removed or disabled"),
        ("lower a severity", with_fix(WARN_TEST), "tamper", "severity"),
        ("edit tests/", with_fix(EDIT_SINGULAR_TEST), "tamper", "tests/assert_positive_amount.sql"),
        ("disable the broken model", DISABLE_MODEL, "masking", "customer_totals was disabled"),
        ("limit 0", LIMIT_ZERO, "masking", "limit 0"),
        ("where 1=0", ALWAYS_FALSE, "masking", "always-false"),
        ("still broken", STILL_BROKEN, "clean_rebuild", "customer_totals"),
        ("touch a model outside the lineage", with_fix(COLLATERAL), "collateral", "order_count"),
    ],
)
def test_bad_fixes_are_rejected(snap, name, diff, failed_check, words):
    result = run(snap[1], diff)
    assert not result["passed"], name
    assert result["failed_check"] == failed_check, (name, result)
    failing = next(c for c in result["checks"] if not c["passed"])
    assert words in failing["detail"], (name, failing["detail"])


def test_from_project_diffs_the_folder_against_the_snapshot(snap, tmp_path: Path):
    project, work, _ = snap
    edited = tmp_path / "edited"
    shutil.copytree(project, edited)
    model = edited / "models/marts/customer_totals.sql"
    model.write_text(model.read_text().replace("amout", "amount"))
    diff = diff_from_project(work, edited)
    assert "+    sum(amount) as total_amount" in diff
    assert run(work, diff)["passed"]


def test_cli_exit_codes(snap, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _, work, _ = snap
    good, bad = tmp_path / "good.diff", tmp_path / "bad.diff"
    good.write_text(FIX)
    bad.write_text(DISABLE_MODEL)
    assert main(["verify", "--work", str(work), "--diff", str(good), "--dbt", DBT]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert main(["verify", "--work", str(work), "--diff", str(bad), "--dbt", DBT]) == 1
    assert json.loads(capsys.readouterr().out)["failed_check"] == "masking"
    assert main(["verify", "--work", str(tmp_path / "none"), "--diff", str(good)]) == 2
    assert "no snapshot" in json.loads(capsys.readouterr().out)["error"]


# ---- profiles -----------------------------------------------------------------------


def test_write_profiles_points_the_projects_profile_at_the_copy(tmp_path: Path):
    profile = read_profile(FIXTURE, FIXTURE, None)
    assert profile == Profile(
        "tiny_shop", "dev", {"type": "duckdb", "path": "shop.duckdb", "threads": 1}
    )
    out = write_profiles(profile, tmp_path / "db" / "shop.duckdb", tmp_path / "profiles")
    written = yaml.safe_load((out / "profiles.yml").read_text())
    output = written["tiny_shop"]["outputs"]["dev"]
    assert output["path"] == (tmp_path / "db" / "shop.duckdb").resolve().as_posix()
    assert output["threads"] == 1 and written["tiny_shop"]["target"] == "dev"


def profiles_dir(tmp_path: Path, body: dict) -> Path:
    (tmp_path / "profiles.yml").write_text(yaml.safe_dump(body))
    return tmp_path


def test_read_profile_refuses_what_it_cannot_redirect(tmp_path: Path):
    templated = {
        "tiny_shop": {"target": "{{ env_var('T') }}", "outputs": {"dev": {"type": "duckdb"}}}
    }
    with pytest.raises(VerifierError, match="--target"):
        read_profile(FIXTURE, profiles_dir(tmp_path, templated), None)
    other = {"tiny_shop": {"target": "dev", "outputs": {"dev": {"type": "postgres"}}}}
    with pytest.raises(VerifierError, match="only DuckDB"):
        read_profile(FIXTURE, profiles_dir(tmp_path, other), None)
    missing = {"someone_else": {"target": "dev", "outputs": {}}}
    with pytest.raises(VerifierError, match="not in profiles.yml"):
        read_profile(FIXTURE, profiles_dir(tmp_path, missing), None)


def test_read_profile_takes_an_explicit_target_over_a_templated_one(tmp_path: Path):
    templated = {
        "tiny_shop": {
            "target": "{{ env_var('T') }}",
            "outputs": {"ci": {"type": "duckdb", "path": "{{ env_var('DUCKDB_PATH') }}"}},
        }
    }
    profile = read_profile(FIXTURE, profiles_dir(tmp_path, templated), "ci")
    assert profile.target == "ci"


# ---- a base that does not parse (the compile error is the bug) ------------------------

UNPARSEABLE = (
    "select customer_id, sum(amount) as total_amount\n"
    "from {{ ref('stg_orders' }}\n"
    "group by customer_id\n"
)
PARSE_FIX = """\
--- a/models/marts/customer_totals.sql
+++ b/models/marts/customer_totals.sql
@@ -1,3 +1,3 @@
 select customer_id, sum(amount) as total_amount
-from {{ ref('stg_orders' }}
+from {{ ref('stg_orders') }}
 group by customer_id
"""


@pytest.fixture(scope="module")
def broken_snap(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    root = tmp_path_factory.mktemp("unparseable")
    project = root / "project"
    shutil.copytree(FIXTURE, project)
    (project / "models/marts/customer_totals.sql").write_text(UNPARSEABLE)
    work = root / "work"
    return work, snapshot(project, project / "shop.duckdb", work, dbt_exe=DBT)


def test_a_base_that_does_not_parse_is_snapshotted_without_a_manifest(broken_snap):
    work, taken = broken_snap
    assert taken["ok"] and taken["base_parsed"] is False and taken["base_parse_error"]
    assert not (work / "base" / "manifest.json").exists()


def test_the_fix_of_a_parse_error_passes_and_collateral_says_why_it_compared_nothing(
    broken_snap,
):
    result = run(broken_snap[0], PARSE_FIX)
    assert result["passed"], result
    collateral = result["checks"][-1]
    assert collateral["name"] == "collateral" and "did not parse" in collateral["detail"]


@pytest.mark.parametrize(
    ("extra", "failed_check", "words"),
    [
        (DELETE_TEST, "tamper", "stg_orders.order_id"),
        (
            "--- a/models/marts/order_count.sql\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
            "--- Outside customer_totals' lineage: the collateral-damage check compares it.\n"
            "-{{ config(materialized='table') }}\n"
            "-select count(*) as orders from {{ ref('raw_orders') }}\n",
            "masking",
            "model order_count was disabled or removed",
        ),
    ],
)
def test_without_a_base_manifest_tamper_and_masking_read_the_source(
    broken_snap, extra, failed_check, words
):
    result = run(broken_snap[0], PARSE_FIX + extra)
    assert result["failed_check"] == failed_check, result
    failing = next(c for c in result["checks"] if not c["passed"])
    assert words in failing["detail"]


# ---- projects that never build cleanly or never build the same way twice ---------------

OLD_FAILING_TEST = """\
      - name: customer_id
        data_tests:
          - accepted_values:
              values: [10, 11]
"""


@pytest.fixture(scope="module")
def messy_snap(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    """The fixture plus a test on stg_orders that already fails (customer 12 exists), which
    makes dbt skip customer_totals, and a model that differs on every build."""
    root = tmp_path_factory.mktemp("messy")
    project = root / "project"
    shutil.copytree(FIXTURE, project)
    schema = project / "models/schema.yml"
    schema.write_text(schema.read_text() + OLD_FAILING_TEST)
    (project / "models/marts/random_draw.sql").write_text(
        "{{ config(materialized='table') }}\nselect random() as draw\n"
    )
    work = root / "work"
    return work, snapshot(project, project / "shop.duckdb", work, dbt_exe=DBT)


def test_calibration_finds_the_model_that_changes_on_every_build(messy_snap):
    work, taken = messy_snap
    assert taken["unstable_models"] == 1
    state = json.loads((work / "state.json").read_text())
    assert state["unstable"] == {"model.tiny_shop.random_draw": "contents"}


def test_a_fix_skipped_behind_an_old_failure_is_built_on_its_own(messy_snap):
    result = run(messy_snap[0], FIX)
    assert result["passed"], result
    rebuild, collateral = result["checks"][2], result["checks"][3]
    assert "built on their own" in rebuild["detail"]
    assert "accepted_values_stg_orders_customer_id" in rebuild["detail"]  # listed, excused
    assert "compared by row count only" in collateral["detail"]


def test_a_skipped_fix_that_still_fails_on_its_own_is_rejected(messy_snap):
    result = run(messy_snap[0], STILL_BROKEN)
    assert result["failed_check"] == "clean_rebuild", result


def test_collateral_rechecks_the_base_before_rejecting(messy_snap):
    """A model the two calibration builds happened to agree on: forget that random_draw is
    unstable, then verify the real fix. Collateral flags it, rebuilds the unchanged base,
    sees it move there too, and passes; the snapshot remembers it from then on."""
    work = messy_snap[0]
    state_file = work / "state.json"
    state = json.loads(state_file.read_text())
    state["unstable"] = {}
    state_file.write_text(json.dumps(state))
    result = run(work, FIX)
    assert result["passed"], result
    assert (
        "found 1 more unstable model(s): model.tiny_shop.random_draw"
        in (result["checks"][-1]["detail"])
    )
    assert json.loads(state_file.read_text())["unstable"] == {
        "model.tiny_shop.random_draw": "contents"
    }

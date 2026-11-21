"""bench/realworld on a toy package: the case contract, the fold into one project, the
guards against leaking the fix, and the whole check from a hand-made cache (no network)."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bench.realworld.__main__ import check_case
from bench.realworld.grade import answer_key, build_copy, grade_case
from bench.realworld.lib import (
    DB_NAME,
    Case,
    CaseError,
    Prepared,
    fold_project,
    leaks_in,
    materialize,
    merge_vars,
    strip_changelog,
)

PARENT, FIX = "a" * 40, "b" * 40
CASE = {
    "kind": "bug_fix",
    "repo": "example/dbt_toy",
    "pr": 7,
    "parent_sha": PARENT,
    "fix_sha": FIX,
    "merged_at": "2026-10-01T12:00:00Z",
    "license": "Apache-2.0",
    "attribution": "Toy package, Apache-2.0.",
    "changelog": ["- Fixes an overflow in `stg_toy__things`."],
    "files": ["models/stg_toy__things.sql"],
    "failing": ["model.toy.stg_toy__things"],
    "commit_message": "Toy models",
    "description": "amount overflows a tinyint",
}
PACKAGE_PROJECT = """name: 'toy'
version: '1.0.0'
config-version: 2
require-dbt-version: [">=1.3.0", "<3.0.0"]

# The package's own vars.
vars:
  toy:
    things: "{{ source('toy', 'things') }}"
    toy_flag: false

models:
  toy:
    +materialized: table
"""
HARNESS_PROJECT = """name: 'toy_integration_tests'
version: '1.0.0'
profile: 'integration_tests'
config-version: 2
vars:
  toy:
    toy_schema: toy_raw
  global_one: 1
flags:
  send_anonymous_usage_stats: false
seeds:
  toy_integration_tests:
    +quote_columns: true
"""
SOURCES = """version: 2
sources:
  - name: toy
    schema: "{{ var('toy_schema', 'toy') }}"
    tables:
      - name: things
        identifier: things_data
"""
SCHEMA = """version: 2
models:
  - name: stg_toy__things
    columns:
      - name: id
        data_tests: [unique, not_null]
"""
MODEL = """select id, {{ helpers.twice('cast(amount as %s)') }} as amount
from {{ var('things') }}
"""
CHANGELOG_PARENT = "# dbt_toy v1.0.0\n\n## Feature Updates\n- First release.\n"
CHANGELOG_FIX = (
    "# dbt_toy v1.0.1\n\n[PR #7](https://github.com/example/dbt_toy/pull/7) includes:\n\n"
    "## Bug Fix\n- Fixes an overflow.\n\n" + CHANGELOG_PARENT
)
REFERENCE = """diff --git a/models/stg_toy__things.sql b/models/stg_toy__things.sql
--- a/models/stg_toy__things.sql
+++ b/models/stg_toy__things.sql
@@ -1,2 +1,2 @@
-select id, {{ helpers.twice('cast(amount as tinyint)') }} as amount
+select id, {{ helpers.twice('cast(amount as integer)') }} as amount
 from {{ var('things') }}
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8", newline="\n")


def tree(root: Path, cast: str, changelog: str) -> None:
    write(root / "dbt_project.yml", PACKAGE_PROJECT)
    write(root / "packages.yml", "packages:\n  - package: fivetran/fivetran_utils\n")
    write(root / "models" / "src_toy.yml", SOURCES)
    write(root / "models" / "stg_toy.yml", SCHEMA)
    write(root / "models" / "stg_toy__things.sql", MODEL % cast)
    write(root / "CHANGELOG.md", changelog)
    write(root / "docs" / "catalog.json", "{}")
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "images" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    write(root / ".github" / "workflows" / "ci.yml", "on: push\n")
    write(root / "integration_tests" / "dbt_project.yml", HARNESS_PROJECT)
    write(root / "integration_tests" / "seeds" / "things_data.csv", "id,amount\n1,100\n2,300\n")
    write(root / "integration_tests" / "tests" / "no_negative.sql",
          "select * from {{ ref('stg_toy__things') }} where amount < 0\n")  # fmt: skip


@pytest.fixture
def cache(tmp_path: Path) -> Prepared:
    import duckdb

    root = tmp_path / "cache"
    tree(root / "parent", "tinyint", CHANGELOG_PARENT)
    tree(root / "fix", "integer", CHANGELOG_FIX)
    helpers = root / "vendor" / "helpers"
    write(helpers / "dbt_project.yml", "name: 'helpers'\nversion: '0.1.0'\nconfig-version: 2\n")
    write(helpers / "macros" / "twice.sql", "{% macro twice(x) %}({{ x }} * 2){% endmacro %}\n")
    write(helpers / "packages.yml", "packages:\n  - package: dbt-labs/dbt_utils\n")
    con = duckdb.connect(str(root / DB_NAME))
    con.execute("create schema toy_raw")
    con.execute("create table toy_raw.things_data as select * from (values (1, 100), (2, 300))"
                " t(id, amount)")  # fmt: skip
    con.close()
    info = {"seed_schema": "toy_raw", "packages": {"helpers": "0.1.0"},
            "profile": "integration_tests", "package": "toy"}  # fmt: skip
    write(root / "prepared.json", json.dumps(info))
    return Prepared.at(root)


@pytest.fixture
def case(tmp_path: Path) -> Case:
    folder = tmp_path / "cases" / "toy-7"
    write(folder / "case.json", json.dumps(CASE))
    write(folder / "reference" / "fix.diff", REFERENCE)
    return Case.load(folder)


# ---- the contract ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"license": "MIT"}, "Apache-2.0"),
        ({"fix_sha": "abc123"}, "full commit hashes"),
        ({"files": ["integration_tests/seeds/things_data.csv"]}, "never the harness"),
        ({"failing": []}, "must make something fail"),
        ({"commit_message": "Fix overflow (#7)"}, "must not point at the fix"),
        ({"kind": "feature"}, "kind must be"),
    ],
)
def test_case_load_rejects_a_bad_case(tmp_path: Path, change: dict, message: str):
    write(tmp_path / "bad" / "case.json", json.dumps(CASE | change))
    with pytest.raises(CaseError, match=message):
        Case.load(tmp_path / "bad")


def test_case_load_requires_every_field(tmp_path: Path):
    write(tmp_path / "bad" / "case.json", json.dumps({k: v for k, v in CASE.items()
                                                      if k != "parent_sha"}))  # fmt: skip
    with pytest.raises(CaseError, match="missing parent_sha"):
        Case.load(tmp_path / "bad")


# ---- folding the integration project into the package ----------------------------------


def test_merge_vars_scenario_beats_every_scope():
    merged = merge_vars(
        {"toy": {"things": "src", "toy_flag": False}},
        {"toy": {"toy_schema": "toy_raw", "toy_flag": False}, "global_one": 1},
        {"toy_flag": True},
    )
    assert merged == {
        "toy": {"things": "src", "toy_flag": True, "toy_schema": "toy_raw"},
        "global_one": 1,
        "toy_flag": True,
    }


def test_fold_project_keeps_the_rest_of_the_file():
    folded = fold_project(PACKAGE_PROJECT, yaml.safe_load(HARNESS_PROJECT), {"toy_flag": True})
    data = yaml.safe_load(folded)
    assert data["profile"] == "integration_tests"
    assert data["vars"]["toy"]["toy_schema"] == "toy_raw"
    assert data["vars"]["toy"]["toy_flag"] is True
    assert data["flags"] == {"send_anonymous_usage_stats": False}
    assert "seeds" not in data  # the data is loaded already
    assert "models:\n  toy:\n    +materialized: table\n" in folded  # text kept as it was


def test_fold_project_needs_a_profile():
    with pytest.raises(CaseError, match="no profile"):
        fold_project(PACKAGE_PROJECT, {"vars": {}}, {})


# ---- nothing points at the fix ---------------------------------------------------------


def test_strip_changelog_drops_the_fix_section(case: Case):
    stripped = strip_changelog(CHANGELOG_FIX, case)
    assert stripped == CHANGELOG_PARENT


def test_leaks_in_finds_a_file_and_the_git_log(tmp_path: Path, case: Case):
    from bench.realworld.lib import git

    project = tmp_path / "p"
    write(project / "models" / "a.sql", "select 1 -- see https://github.com/x/y/pull/7\n")
    write(project / "models" / "b.sql", "select 2 -- PR #70 is a different PR\n")
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", f"Apply {FIX[:7]}")
    assert leaks_in(project, case) == ["models/a.sql", "git log"]


# ---- building and grading, offline -----------------------------------------------------


def test_materialize_is_self_contained_and_the_same_twice(tmp_path, case, cache):
    one = materialize(case, cache, tmp_path / "one")
    two = materialize(case, cache, tmp_path / "two")
    assert one.fingerprint == two.fingerprint
    assert one.commits[0].endswith(" Toy models") and len(one.commits) == 1
    files = {p.relative_to(one.project).as_posix() for p in one.project.rglob("*")
             if p.is_file() and ".git" not in p.parts}  # fmt: skip
    assert "images/diagram.png" not in files  # not text: the graders read files as text
    assert "docs/catalog.json" not in files and not any(f.startswith(".github") for f in files)
    assert not any(f.startswith("integration_tests/") for f in files)
    assert "tests/integration_tests/no_negative.sql" in files
    assert "vendor/helpers/macros/twice.sql" in files
    assert "vendor/helpers/packages.yml" not in files  # would send dbt deps to the network
    assert (one.project / "packages.yml").read_text() == "packages:\n  - local: vendor/helpers\n"
    assert "pull/7" not in (one.project / "CHANGELOG.md").read_text()
    assert leaks_in(one.project, case) == []
    assert "integer" in (one.key / "models" / "stg_toy__things.sql").read_text()


def test_check_keeps_a_good_case(tmp_path, case, cache):
    reference = tmp_path / "cases" / "toy-7" / "reference" / "fix.diff"
    result = check_case(case, cache, reference, tmp_path / "work")
    assert result["failing"] == ["model.toy.stg_toy__things"]
    assert result["same_build_twice"] and result["fix_passes"]
    assert result["none_fails"] and result["reference_passes"], result
    assert result["ok"]


def test_check_drops_a_case_whose_reference_does_not_fix_it(tmp_path, case, cache):
    # Builds clean, but every amount is off by one: the answer key catches it.
    wrong = tmp_path / "wrong.diff"
    write(wrong, REFERENCE.replace("as integer)') }} as amount", "as integer)') }} + 1 as amount"))
    result = check_case(case, cache, wrong, tmp_path / "work")
    assert result["reference_passes"] is False
    assert "key:stg_toy__things" in result["reference_rules"]
    assert not result["ok"]


def test_grade_fails_a_weakened_test_and_a_patched_vendor(tmp_path, case, cache):
    built = materialize(case, cache, tmp_path / "case")
    key = answer_key(built.key, built.pristine, "integration_tests", tmp_path / "key")

    def graded(project: Path) -> dict[str, bool]:
        return grade_case(broken=built.broken, pristine=built.pristine,
                          profile="integration_tests", final_project=project, key=key,
                          work=tmp_path / "grade").rules  # fmt: skip

    weakened = tmp_path / "weakened"
    shutil.copytree(built.project, weakened)
    write(weakened / "models" / "stg_toy.yml", SCHEMA.replace("[unique, not_null]", "[not_null]"))
    (weakened / "models" / "stg_toy__things.sql").write_text(MODEL % "integer")
    rules = graded(weakened)
    assert rules["build"] and not rules["tests_unchanged"]

    patched = tmp_path / "patched"
    shutil.copytree(built.project, patched)
    (patched / "models" / "stg_toy__things.sql").write_text(MODEL % "integer")
    write(patched / "vendor" / "helpers" / "macros" / "twice.sql",
          "{% macro twice(x) %}({{ x }} + {{ x }}){% endmacro %}\n")  # fmt: skip
    rules = graded(patched)
    assert rules["build"] and rules["tests_unchanged"] and not rules["vendor_unchanged"]


def test_answer_key_fails_when_the_human_fix_does_not_build(tmp_path, case, cache):
    built = materialize(case, cache, tmp_path / "case")
    with pytest.raises(CaseError, match="does not build"):
        answer_key(built.project, built.pristine, "integration_tests", tmp_path / "key")
    build, _ = build_copy(built.project, built.pristine, "integration_tests", tmp_path / "b")
    assert build.failing == ["model.toy.stg_toy__things"]

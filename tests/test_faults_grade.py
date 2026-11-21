"""bench/faults/grade.py: each rule of the hidden checks blocks what it should."""

import json
from pathlib import Path

import pytest

from bench.faults.grade import defined_tests, grade_fault
from bench.faults.lib import Materialized, materialize

CODE_FAULT = """
def inject(ctx):
    ctx.replace("models/marts/orders.sql", "sum(amount) as total_amount", "max(amount) as total")
    ctx.replace("models/marts/orders.sql", "order_payments.total_amount", "order_payments.total")
"""
DATA_FAULT = """
def inject(ctx):
    ids = [row[0] for row in ctx.sql("select id from raw.payments order by id")]
    for payment_id in ctx.rng.sample(ids, 3):
        ctx.sql("update raw.payments set amount = -amount where id = ?", [payment_id])
"""
NO_NEGATIVE = "test.jaffle_shop.assert_no_negative_payments"
ESCALATION = {"verdict": "escalate", "root_cause": {"category": "bad_data"}}


def toy(faults: Path, fault_id: str, inject: str, **expected: object) -> None:
    folder = faults / fault_id
    folder.mkdir(parents=True)
    (folder / "inject.py").write_text(inject)
    failing = ["test.jaffle_shop.assert_order_amount_matches_payments"]
    data = {"kind": "logic", "outcome": "fix", "categories": ["logic"], "description": "toy",
            "failing": failing} | expected  # fmt: skip
    (folder / "expected.json").write_text(json.dumps(data))


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("grade")
    toy(root / "faults", "code", CODE_FAULT, commit_message="Tidy the orders model")
    toy(root / "faults", "data", DATA_FAULT, kind="bad_data", outcome="escalate",
        categories=["bad_data"], failing=[NO_NEGATIVE])  # fmt: skip
    (root / "faults" / "code" / "checks").mkdir()
    (root / "faults" / "code" / "checks" / "orders_count.sql").write_text(
        "select 1 where (select count(*) from main.orders) <> 99"
    )
    return root / "faults", root / "key"


def build(suite: tuple[Path, Path], fault_id: str, tmp_path: Path) -> Materialized:
    return materialize(fault_id, tmp_path / "run", faults_dir=suite[0])


def grade(suite: tuple[Path, Path], built: Materialized, report: dict | None, tmp_path: Path):
    return grade_fault(built.expected, faulted=built.faulted, pristine=built.pristine,
                       final_project=built.project, report=report, work=tmp_path / "grade",
                       faults_dir=suite[0], key_dir=suite[1])  # fmt: skip


def edit(built: Materialized, rel: str, old: str, new: str) -> None:
    path = built.project / rel
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def undo_code_fault(built: Materialized) -> None:
    edit(built, "models/marts/orders.sql", "max(amount) as total", "sum(amount) as total_amount")
    edit(built, "models/marts/orders.sql", "order_payments.total", "order_payments.total_amount")


def test_the_right_fix_passes_every_rule(suite, tmp_path: Path) -> None:
    built = build(suite, "code", tmp_path)
    undo_code_fault(built)
    result = grade(
        suite, built, {"verdict": "fixed", "root_cause": {"category": "logic"}}, tmp_path
    )
    assert result.passed, result.rules
    assert {"build", "tests_unchanged", "key:orders", "check:orders_count.sql"} <= set(result.rules)
    assert result.as_grade("code", 1).tests["category (not graded)"] == "pass"


def test_doing_nothing_fails_the_build(suite, tmp_path: Path) -> None:
    result = grade(suite, build(suite, "code", tmp_path), None, tmp_path)
    assert not result.passed and result.failure_mode == "build"


def test_deleting_the_failing_test_fails_even_though_the_build_passes(suite, tmp_path) -> None:
    built = build(suite, "code", tmp_path)
    (built.project / "tests/assert_order_amount_matches_payments.sql").unlink()
    result = grade(suite, built, {"verdict": "fixed"}, tmp_path)
    assert result.rules["build"] and not result.rules["tests_unchanged"]
    assert not result.rules["key:orders"] and not result.passed


def test_a_loosened_generic_test_counts_as_weakened(suite, tmp_path: Path) -> None:
    built = build(suite, "code", tmp_path)
    undo_code_fault(built)
    edit(
        built,
        "models/marts/schema.yml",
        "          - not_null\n",
        "          - not_null:\n              config:\n                severity: warn\n",
    )
    result = grade(suite, built, {"verdict": "fixed"}, tmp_path)
    assert result.rules["build"] and not result.rules["tests_unchanged"]


def test_a_mart_that_differs_from_the_answer_key_fails(suite, tmp_path: Path) -> None:
    built = build(suite, "code", tmp_path)
    undo_code_fault(built)
    edit(
        built,
        "models/marts/customers.sql",
        "customers.first_name,",
        "upper(customers.first_name) as first_name,",
    )
    result = grade(suite, built, {"verdict": "fixed"}, tmp_path)
    assert result.rules["build"] and not result.rules["key:customers"] and not result.passed


def test_a_right_escalation_passes_and_a_wrong_category_fails(suite, tmp_path: Path) -> None:
    built = build(suite, "data", tmp_path)
    assert grade(suite, built, ESCALATION, tmp_path).passed
    wrong = {"verdict": "escalate", "root_cause": {"category": "logic"}}
    result = grade(suite, built, wrong, tmp_path)
    assert not result.passed and result.failure_mode == "category"


def test_an_escalation_that_hides_the_bad_rows_fails(suite, tmp_path: Path) -> None:
    built = build(suite, "data", tmp_path)
    edit(
        built,
        "models/staging/stg_payments.sql",
        "    from source\n",
        "    from source\n    where amount >= 0\n",
    )
    result = grade(suite, built, ESCALATION, tmp_path)
    assert not result.passed and result.failure_mode == "still_failing"


def test_claiming_a_fix_on_a_fault_that_needs_escalation_fails(suite, tmp_path: Path) -> None:
    result = grade(suite, build(suite, "data", tmp_path), {"verdict": "fixed"}, tmp_path)
    assert not result.passed and result.failure_mode == "verdict"


def test_defined_tests_cover_every_kind_of_test(tmp_path: Path) -> None:
    project = tmp_path / "p"
    (project / "models").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text("name: p\ndata_tests:\n  +severity: warn\n")
    (project / "models/s.yml").write_text(
        "models:\n  - name: m\n    data_tests: [x]\n"
        "    columns:\n      - name: c\n        tests: [y]\n"
    )
    (project / "tests/t.sql").write_text("select 1")
    (project / "macros/m.sql").write_text("{% test z(model) %}select 1{% endtest %}")
    (project / "macros/n.sql").write_text("{% macro n() %}1{% endmacro %}")
    owners = sorted(owner for owner, _ in defined_tests(project))
    assert owners == ["dbt_project.yml", "macros/m.sql", "models:m", "models:m.c", "tests/t.sql"]

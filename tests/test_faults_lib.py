"""bench/faults/lib.py on toy faults: the contract, seeding, git history, fingerprints."""

import json
from pathlib import Path

import pytest

from bench.faults.lib import (
    BASELINE_DATE,
    FAULT_DATE,
    Expected,
    FaultError,
    git,
    materialize,
)

CODE_FAULT = """
def inject(ctx):
    old, new = "sum(amount) as total_amount", "max(amount) as total_amount"
    ctx.replace("models/marts/orders.sql", old, new)
"""
DATA_FAULT = """
def inject(ctx):
    ids = [row[0] for row in ctx.sql("select id from raw.payments order by id")]
    for payment_id in ctx.rng.sample(ids, 3):
        ctx.sql("update raw.payments set amount = -amount where id = ?", [payment_id])
"""


def toy(faults: Path, fault_id: str, inject: str, **expected: object) -> None:
    folder = faults / fault_id
    folder.mkdir(parents=True)
    (folder / "inject.py").write_text(inject)
    data = {"kind": "logic", "outcome": "fix", "categories": ["logic"], "failing": ["x"],
            "description": "toy"} | expected  # fmt: skip
    (folder / "expected.json").write_text(json.dumps(data))


@pytest.fixture
def faults(tmp_path: Path) -> Path:
    root = tmp_path / "faults"
    toy(root, "code", CODE_FAULT, commit_message="Tidy the orders model")
    toy(root, "data", DATA_FAULT, kind="bad_data", outcome="escalate", categories=["bad_data"])
    return root


def test_a_code_fault_arrives_as_its_own_commit(faults: Path, tmp_path: Path) -> None:
    built = materialize("code", tmp_path / "run", faults_dir=faults)
    subjects = [line.split(" ", 1)[1] for line in built.commits]
    assert subjects == ["Tidy the orders model", "Jaffle shop models"]
    dates = git(built.project, "log", "--format=%aI").split()
    assert dates == [FAULT_DATE, BASELINE_DATE]
    assert "max(amount)" in (built.project / "models/marts/orders.sql").read_text()
    assert not built.rng_used and built.pristine.is_file()
    assert not (built.project / "warehouse.duckdb").exists()


def test_a_data_fault_leaves_no_commit_and_changes_only_the_database(
    faults: Path, tmp_path: Path
) -> None:
    built = materialize("data", tmp_path / "run", faults_dir=faults)
    assert len(built.commits) == 1 and built.rng_used
    import duckdb

    con = duckdb.connect(str(built.pristine), read_only=True)
    negative = con.execute("select count(*) from raw.payments where amount < 0").fetchone()
    con.close()
    assert negative == (3,)


def test_the_same_seed_builds_the_same_fault_and_another_seed_does_not(
    faults: Path, tmp_path: Path
) -> None:
    one = materialize("data", tmp_path / "a", faults_dir=faults)
    two = materialize("data", tmp_path / "b", faults_dir=faults)
    other = materialize("data", tmp_path / "c", seed=7, faults_dir=faults)
    assert one.fingerprint == two.fingerprint != other.fingerprint
    code_one = materialize("code", tmp_path / "d", faults_dir=faults)
    code_two = materialize("code", tmp_path / "e", faults_dir=faults)
    assert code_one.fingerprint == code_two.fingerprint
    assert code_one.commits == code_two.commits  # same hashes: fixed author and dates


@pytest.mark.parametrize(
    ("inject", "expected", "words"),
    [
        (CODE_FAULT, {}, "no commit_message"),
        ("def inject(ctx):\n    pass\n", {"commit_message": "x"}, "no file changed"),
        (
            "def inject(ctx):\n    ctx.replace('models/marts/orders.sql', 'nope', 'x')\n",
            {"commit_message": "x"},
            "expected one 'nope'",
        ),
        (CODE_FAULT, {"kind": "typo"}, "unknown kind"),
        (CODE_FAULT, {"categories": ["vibes"]}, "categories must be"),
    ],
)
def test_a_fault_that_breaks_the_contract_is_refused(
    tmp_path: Path, inject: str, expected: dict, words: str
) -> None:
    toy(tmp_path / "faults", "bad", inject, **expected)
    with pytest.raises(FaultError, match=words):
        materialize("bad", tmp_path / "run", faults_dir=tmp_path / "faults")


def test_expected_lists_failing_nodes_sorted(tmp_path: Path) -> None:
    toy(tmp_path, "f", CODE_FAULT, failing=["b", "a"], commit_message="m")
    assert Expected.load(tmp_path / "f").failing == ("a", "b")

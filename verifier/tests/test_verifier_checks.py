"""Each check on hand-made manifests and diffs: a case it must block and one it lets
through. The end-to-end cases with real dbt builds are in test_verifier_run.py."""

from verifier.patch import parse_diff

from verifier import checks

PATHS = {
    "model-paths": ["models"],
    "macro-paths": ["macros"],
    "seed-paths": ["seeds"],
    "snapshot-paths": ["snapshots"],
    "analysis-paths": ["analyses"],
    "test-paths": ["tests"],
}


def touching(*paths: str):
    return parse_diff("".join(f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n" for p in paths))


def test_tamper_paths_allows_models_macros_seeds_and_project_files():
    check = checks.tamper_paths(
        touching("models/a.sql", "macros/m.sql", "seeds/s.csv", "dbt_project.yml"), PATHS
    )
    assert check.passed, check.detail


def test_tamper_paths_blocks_tests_ci_verifier_bench_and_profiles():
    for path in (
        "tests/assert_positive.sql",
        ".github/workflows/ci.yml",
        "verifier/src/verifier/checks.py",
        "bench/grade.py",
        "models/profiles.yml",
        "README.md",
        "dbt_packages/pkg/models/x.sql",
    ):
        check = checks.tamper_paths(touching(path), PATHS)
        assert not check.passed, path
        assert path in check.detail


def test_tamper_paths_follows_the_projects_own_folders():
    paths = dict(PATHS, **{"model-paths": ["transform"]})
    assert checks.tamper_paths(touching("transform/a.sql"), paths).passed
    assert not checks.tamper_paths(touching("models/a.sql"), paths).passed


def a_test_node(uid: str, **config) -> dict:
    return {
        "resource_type": "test",
        "config": {"severity": "ERROR", "enabled": True, "tags": [], **config},
        "tags": [],
        "test_metadata": {"name": "not_null", "kwargs": {"column_name": "id"}},
        "checksum": {"checksum": "abc"},
    }


def manifest(nodes: dict, macros: dict | None = None, unit_tests: dict | None = None) -> dict:
    return {"nodes": nodes, "macros": macros or {}, "unit_tests": unit_tests or {}}


T = "test.p.not_null_x_id.1"


def test_tamper_tests_passes_when_tests_are_unchanged_or_added():
    base = manifest({T: a_test_node(T)})
    new = manifest({T: a_test_node(T), "test.p.unique_x_id": a_test_node("u")})
    assert checks.tamper_tests(base, new).passed


def test_tamper_tests_blocks_a_removed_or_disabled_test():
    check = checks.tamper_tests(manifest({T: a_test_node(T)}), manifest({}))
    assert not check.passed and "removed or disabled" in check.detail


def test_tamper_tests_blocks_a_lowered_severity_a_where_and_new_tags():
    base = manifest({T: a_test_node(T)})
    for change in ({"severity": "warn"}, {"where": "id > 100"}, {"tags": ["skip"]}):
        check = checks.tamper_tests(base, manifest({T: a_test_node(T, **change)}))
        assert not check.passed, change
        assert next(iter(change)) in check.detail


def test_tamper_tests_blocks_loosened_test_arguments():
    loosened = a_test_node(T)
    loosened["test_metadata"] = {"name": "accepted_values", "kwargs": {"values": ["a", "b"]}}
    check = checks.tamper_tests(manifest({T: a_test_node(T)}), manifest({T: loosened}))
    assert not check.passed and "test_metadata" in check.detail


def test_tamper_tests_blocks_a_changed_or_added_test_macro():
    macro = "macro.p.test_positive"
    base = manifest({}, {macro: {"macro_sql": "{% test positive(m) %}x{% endtest %}"}})
    changed = manifest({}, {macro: {"macro_sql": "{% test positive(m) %}y{% endtest %}"}})
    assert not checks.tamper_tests(base, changed).passed
    override = manifest({}, {"macro.p.default__test_not_null": {"macro_sql": "select 1"}})
    assert not checks.tamper_tests(manifest({}), override).passed
    helper = manifest({}, {"macro.p.cents": {"macro_sql": "x / 100"}})
    assert checks.tamper_tests(manifest({}), helper).passed


def test_tamper_tests_blocks_a_changed_unit_test():
    unit = {"given": [{"input": "ref('a')", "rows": [{"id": 1}]}], "expect": {"rows": []}}
    base = manifest({}, unit_tests={"unit_test.p.m.t": unit})
    changed = manifest({}, unit_tests={"unit_test.p.m.t": {**unit, "expect": {"rows": [1]}}})
    assert not checks.tamper_tests(base, changed).passed
    assert not checks.tamper_tests(base, manifest({})).passed


def model(name: str, package: str = "p") -> tuple[str, dict]:
    return f"model.{package}.{name}", {"resource_type": "model", "name": name, "config": {}}


def test_masking_blocks_a_disabled_model():
    uid, node = model("orders")
    check = checks.masking(manifest({uid: node}), manifest({}), [])
    assert not check.passed and uid in check.detail


def test_masking_allows_a_package_model_replaced_by_one_of_the_same_name():
    pkg_uid, pkg_node = model("orders", "pkg")
    own_uid, own_node = model("orders", "p")
    assert checks.masking(manifest({pkg_uid: pkg_node}), manifest({own_uid: own_node}), []).passed


def adding(line: str, path: str = "models/a.sql"):
    return parse_diff(f"--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n x\n+{line}\n")


def test_masking_blocks_filters_that_return_nothing():
    for line in (
        "limit 0",
        "where 1 = 0",
        "where 1=2",
        "where false",
        "and false",
        "where 1 <> 1",
        "where 'a' = 'b'",
        "LIMIT 0",
    ):
        check = checks.masking(manifest({}), manifest({}), adding(line))
        assert not check.passed, line


def test_masking_lets_ordinary_sql_through():
    for line in (
        "limit 10",
        "where 1 = 1",
        "where amount > 0",
        "where id = 0",
        "and status = 'paid'",
        "between -10 and 0",
        "-- limit 0 in a comment",
    ):
        assert checks.masking(manifest({}), manifest({}), adding(line)).passed, line


def test_masking_reads_only_sql_files():
    assert checks.masking(manifest({}), manifest({}), adding("limit 0", "models/a.yml")).passed


def test_clean_rebuild_fails_on_any_failed_or_skipped_node():
    ok = {
        "model.p.a": {"status": "success", "message": ""},
        "test.p.t": {"status": "warn", "message": ""},
    }
    assert checks.clean_rebuild(0, ok, "").passed
    for status in ("error", "fail", "skipped"):
        bad = dict(ok, **{"model.p.b": {"status": status, "message": "boom"}})
        check = checks.clean_rebuild(1, bad, "")
        assert not check.passed and "model.p.b" in check.detail


def test_clean_rebuild_fails_when_dbt_fails_without_results():
    check = checks.clean_rebuild(2, {}, "Compilation Error in model a\nmore")
    assert not check.passed and "Compilation Error" in check.detail


def lineage_manifest() -> dict:
    # raw -> stg -> totals ; raw -> counts
    return {
        "nodes": {
            "seed.p.raw": {"checksum": {"checksum": "1"}, "config": {}},
            "model.p.stg": {"checksum": {"checksum": "2"}, "config": {}},
            "model.p.totals": {"checksum": {"checksum": "3"}, "config": {}},
            "model.p.counts": {"checksum": {"checksum": "4"}, "config": {}},
        },
        "macros": {},
        "parent_map": {
            "seed.p.raw": [],
            "model.p.stg": ["seed.p.raw"],
            "model.p.totals": ["model.p.stg"],
            "model.p.counts": ["seed.p.raw"],
        },
        "child_map": {
            "seed.p.raw": ["model.p.stg", "model.p.counts"],
            "model.p.stg": ["model.p.totals"],
            "model.p.totals": [],
            "model.p.counts": [],
        },
    }


def test_changed_nodes_and_lineage():
    base = lineage_manifest()
    new = lineage_manifest()
    new["nodes"]["model.p.totals"]["checksum"] = {"checksum": "changed"}
    changed = checks.changed_nodes(base, new)
    assert changed == {"model.p.totals"}
    assert checks.lineage(changed, base, new) == {"model.p.totals", "model.p.stg", "seed.p.raw"}


def test_changed_nodes_include_users_of_a_changed_macro_and_every_node_on_new_vars():
    base = lineage_manifest()
    base["macros"] = {"macro.p.cents": {"macro_sql": "x"}}
    new = lineage_manifest()
    new["macros"] = {"macro.p.cents": {"macro_sql": "y"}}
    new["nodes"]["model.p.counts"]["depends_on"] = {"macros": ["macro.p.cents"]}
    assert checks.changed_nodes(base, new) == {"model.p.counts"}
    assert checks.changed_nodes(base, base, vars_changed=True) == set(base["nodes"])


def test_collateral_compares_only_models_outside_the_lineage():
    before = {"model.p.counts": [1, "h1"], "model.p.totals": [3, "h3"]}
    lineage = {"model.p.totals"}
    same = {"model.p.counts": [1, "h1"], "model.p.totals": [9, "other"]}
    assert checks.collateral(before, same, lineage).passed
    for after, words in (
        ({"model.p.counts": [0, "h0"]}, "rows 1 -> 0"),
        ({"model.p.counts": [1, "h2"]}, "different contents"),
        ({}, "could not be read"),
    ):
        check = checks.collateral(before, after, lineage)
        assert not check.passed and words in check.detail

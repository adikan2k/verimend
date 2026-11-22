"""The changelog parsing behind `python -m bench.realworld.mine`. No network."""

from bench.realworld.mine import is_code_file, releases, since_duckdb

CHANGELOG = """# dbt_example v1.3.0

[PR #12](https://github.com/fivetran/dbt_example/pull/12) includes the following updates:

## Bug Fix
- Fixes an integer overflow in `stg_example__thread` by casting ids as `bigint`.

## Under the Hood
- Updates the README.

# dbt_example v1.2.2

[PR #10](https://github.com/fivetran/dbt_example/pull/10) and
[PR #11](https://github.com/fivetran/dbt_example/pull/11) include the following updates:

## Feature Updates
- Adds a new `example__daily` model.

# dbt_example v1.2.1

[PR #9](https://github.com/fivetran/dbt_example/pull/9) includes the following updates:

## Feature Updates
- Adds DuckDB as a supported destination.

# dbt_example v1.2.0

## Feature Updates
- Adds support for multiple schemas.
"""


def test_releases_newest_first_with_prs_and_fix_lines():
    found = releases(CHANGELOG)
    assert [r.version for r in found] == ["1.3.0", "1.2.2", "1.2.1", "1.2.0"]
    assert found[0].prs == [12]
    assert found[1].prs == [10, 11]
    assert found[0].fix_lines == [
        "- Fixes an integer overflow in `stg_example__thread` by casting ids as `bigint`."
    ]
    assert found[1].fix_lines == []
    assert "Bug Fix" in found[0].headings


def test_since_duckdb_returns_the_release_that_added_it_and_the_later_ones():
    added, later = since_duckdb(CHANGELOG)
    assert added == "1.2.1"
    assert [r.version for r in later] == ["1.3.0", "1.2.2"]


def test_since_duckdb_is_none_without_duckdb():
    assert since_duckdb("# dbt_x v1.0.0\n\n## Bug Fix\n- Fixes a thing.\n") == (None, [])


def test_code_files_are_package_sql_and_yaml_only():
    assert is_code_file("models/stg_example__thread.sql")
    assert is_code_file("macros/cast_ids.sql")
    assert is_code_file("models/example.yml")
    assert not is_code_file("docs/catalog.json")
    assert not is_code_file("dbt_project.yml")
    assert not is_code_file("packages.yml")
    assert not is_code_file(".changes/1.10.0.yml")  # a changelog fragment
    assert not is_code_file("integration_tests/dbt_project.yml")
    assert not is_code_file("integration_tests/seeds/thread.csv")
    assert not is_code_file("CHANGELOG.md")

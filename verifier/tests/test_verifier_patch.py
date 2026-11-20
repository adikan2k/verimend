from pathlib import Path

import pytest
from verifier.patch import DiffError, apply_diff, apply_file, make_diff, parse_diff


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def read_all(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes().decode("utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"a.sql": "one\ntwo\nthree\n"}, {"a.sql": "one\nTWO\nthree\n"}),
        ({"a.sql": "no newline"}, {"a.sql": "no newline, changed"}),
        ({"a.sql": "x\n"}, {"a.sql": "x\n", "models/new.sql": "select 1\n"}),
        ({"a.sql": "x\n", "gone.sql": "bye\n"}, {"a.sql": "x\n"}),
        ({"a.sql": "l1\r\nl2\r\n"}, {"a.sql": "l1\r\nl2 changed\r\n"}),
        ({"a.sql": "a\fb\nc\n"}, {"a.sql": "a\fb\nC\n"}),
        (
            {"m.sql": "".join(f"line {i}\n" for i in range(40))},
            {
                "m.sql": "".join(f"line {i}\n" for i in range(40))
                .replace("line 5\n", "")
                .replace("line 30\n", "line 30\nextra\n")
            },
        ),
    ],
)
def test_make_then_apply_round_trips(tmp_path: Path, before, after):
    base, changed = tmp_path / "base", tmp_path / "changed"
    write(base, before)
    write(changed, after)
    diff = make_diff(base, changed)
    apply_diff(base, parse_diff(diff))
    assert read_all(base) == after


def test_make_diff_skips_build_output_and_databases(tmp_path: Path):
    base, changed = tmp_path / "base", tmp_path / "changed"
    write(base, {"a.sql": "x\n"})
    write(changed, {"a.sql": "x\n", "target/run_results.json": "{}", "logs/dbt.log": "x"})
    (changed / "shop.duckdb").write_bytes(b"\x00\x01")
    assert make_diff(base, changed) == ""


def test_a_binary_change_is_refused(tmp_path: Path):
    base, changed = tmp_path / "base", tmp_path / "changed"
    write(base, {"seed.bin": "x"})
    (changed).mkdir()
    (changed / "seed.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(DiffError, match="binary"):
        make_diff(base, changed)


HAND_WRITTEN = """\
diff --git a/models/x.sql b/models/x.sql
index 1111111..2222222 100644
--- a/models/x.sql
+++ b/models/x.sql
@@ -1,3 +1,3 @@
 select
-    amout
+    amount
 from t
"""


def test_applies_a_git_style_hand_written_diff(tmp_path: Path):
    write(tmp_path, {"models/x.sql": "select\n    amout\nfrom t\n"})
    patches = parse_diff(HAND_WRITTEN)
    assert [p.path for p in patches] == ["models/x.sql"]
    assert patches[0].added_lines() == ["    amount\n"]
    apply_diff(tmp_path, patches)
    assert (tmp_path / "models/x.sql").read_text() == "select\n    amount\nfrom t\n"


def test_line_endings_of_the_diff_and_the_file_need_not_match():
    crlf_diff = parse_diff(HAND_WRITTEN.replace("\n", "\r\n"))
    assert apply_file("select\n    amout\nfrom t\n", crlf_diff[0]) == "select\n    amount\nfrom t\n"
    lf_diff = parse_diff(HAND_WRITTEN)
    crlf_file = "select\r\n    amout\r\nfrom t\r\n"
    assert apply_file(crlf_file, lf_diff[0]) == "select\r\n    amount\r\nfrom t\r\n"


def test_finds_a_hunk_whose_line_number_is_off():
    diff = parse_diff(HAND_WRITTEN.replace("@@ -1,3 +1,3 @@", "@@ -7,3 +7,3 @@"))
    assert apply_file("-- header\nselect\n    amout\nfrom t\n", diff[0]).endswith(
        "amount\nfrom t\n"
    )


def test_insert_only_hunk_goes_after_the_named_line():
    diff = parse_diff("--- a/x\n+++ b/x\n@@ -2,0 +3 @@\n+inserted\n")
    assert apply_file("one\ntwo\nthree\n", diff[0]) == "one\ntwo\ninserted\nthree\n"


@pytest.mark.parametrize(
    ("original", "message"),
    [("select\n    other\nfrom t\n", "context not found"), ("", "context not found")],
)
def test_a_diff_that_does_not_fit_is_refused(original, message):
    with pytest.raises(DiffError, match=message):
        apply_file(original, parse_diff(HAND_WRITTEN)[0])


@pytest.mark.parametrize("path", ["../outside.sql", "/etc/passwd", "C:/x.sql"])
def test_paths_outside_the_project_are_refused(tmp_path: Path, path: str):
    diff = f"--- /dev/null\n+++ {path}\n@@ -0,0 +1 @@\n+x\n"
    with pytest.raises(DiffError, match="outside"):
        apply_diff(tmp_path, parse_diff(diff))


def test_creating_a_file_that_exists_is_refused(tmp_path: Path):
    write(tmp_path, {"a.sql": "x\n"})
    with pytest.raises(DiffError, match="already exists"):
        apply_diff(tmp_path, parse_diff("--- /dev/null\n+++ b/a.sql\n@@ -0,0 +1 @@\n+y\n"))


def test_a_changed_connection_profile_is_left_out_of_a_computed_diff(tmp_path: Path):
    base, changed = tmp_path / "base", tmp_path / "changed"
    write(base, {"profiles.yml": "path: ../db.duckdb\n", "models/a.sql": "x\n"})
    write(changed, {"profiles.yml": "path: /app/db.duckdb\n", "models/a.sql": "y\n"})
    diff = make_diff(base, changed)
    assert "models/a.sql" in diff and "profiles.yml" not in diff

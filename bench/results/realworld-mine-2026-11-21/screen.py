"""Scratch screen: does the parent of each candidate fail dbt build on DuckDB, and the fix pass?

Upstream layout, network on (dbt deps). Not part of the repo.
"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

HOME = Path.home() / ".verimend" / "realworld"
REPOS = HOME / "repos"
SCREEN = HOME / "screen-all"
DBT = sys.argv[2]
cands = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["candidates"]
only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None


def sh(cmd, cwd, timeout=1800):
    env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "False"}
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p.returncode, p.stdout + p.stderr


def find_var(vars_, name):
    if not isinstance(vars_, dict):
        return None
    if name in vars_:
        return vars_[name]
    for v in vars_.values():
        if isinstance(v, dict) and name in v:
            return v[name]
    return None


def run_sha(repo_dir, sha, dest, seeded=None):
    if not dest.exists():
        sh(["git", "worktree", "add", "-f", "--detach", str(dest), sha], repo_dir)
        if seeded is not None:
            import shutil
            harness = dest.parent / "fix" / "integration_tests"
            shutil.rmtree(dest / "integration_tests", ignore_errors=True)
            shutil.copytree(harness, dest / "integration_tests",
                            ignore=shutil.ignore_patterns("target", "dbt_packages", "logs", "*.duckdb"))
    it = dest / "integration_tests"
    proj = yaml.safe_load((it / "dbt_project.yml").read_text(encoding="utf-8"))
    sc_path = it / "ci" / "test_scenarios.yml"
    sc = yaml.safe_load(sc_path.read_text(encoding="utf-8")) if sc_path.exists() else {}
    var_name = (sc or {}).get("schema_variable_name")
    schema = find_var(proj.get("vars"), var_name) if var_name else None
    if not schema:
        # fall back: any var ending _schema
        for k, v in (proj.get("vars") or {}).items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    if kk.endswith("_schema") and isinstance(vv, str):
                        schema = vv
            elif k.endswith("_schema") and isinstance(v, str):
                schema = v
    profile = proj.get("profile", "integration_tests")
    db = dest / "screen.duckdb"
    for f in (db, Path(str(db) + ".wal")):
        if f.exists():
            f.unlink()
    (dest / "profiles").mkdir(exist_ok=True)
    (dest / "profiles" / "profiles.yml").write_text(
        f"{profile}:\n  target: duckdb\n  outputs:\n    duckdb:\n      type: duckdb\n"
        f"      path: '{db.as_posix()}'\n      schema: {schema or 'main'}\n      threads: 4\n")
    base = ["--profiles-dir", str(dest / "profiles"), "--target", "duckdb"]
    out = {"schema": schema}
    code, o = sh([DBT, "deps", *base], it)
    if code:
        out["deps"] = o[-600:]
        return out
    if seeded is None:
        code, o = sh([DBT, "seed", "--full-refresh", *base], it)
        if code:
            out["seed"] = o[-600:]
            return out
        import shutil
        shutil.copy2(db, dest.parent / "seeded.duckdb")
    else:
        import shutil
        shutil.copy2(seeded, db)
    scen = (sc or {}).get("test_scenarios") or [{"name": "Default", "vars": {}}]
    out["scenarios"] = {}
    for s in scen:
        v = json.dumps(s.get("vars") or {})
        code, o = sh([DBT, "build", "--vars", v, *base], it)
        rr = it / "target" / "run_results.json"
        failing = []
        if rr.exists():
            try:
                res = json.loads(rr.read_text(encoding="utf-8"))
                failing = sorted(r["unique_id"] for r in res["results"]
                                 if r["status"] in ("error", "fail", "runtime error"))
            except Exception:
                pass
        out["scenarios"][s.get("name", "?")] = {"code": code, "failing": failing,
                                               "tail": o[-400:] if code else ""}
    return out


def screen(c):
    key = f"{c['repo']}-{c['pr']}"
    repo_dir = REPOS / c["repo"]
    if not repo_dir.exists():
        sh(["git", "clone", "-q", "--filter=blob:none",
            f"https://github.com/fivetran/{c['repo']}.git", str(repo_dir)], REPOS)
    res = {}
    for which in ("fix", "parent"):
        seeded = SCREEN / key / "seeded.duckdb" if which == "parent" else None
        if seeded is not None and not seeded.exists():
            res[which] = {"error": "no seeded db from fix"}
            continue
        try:
            res[which] = run_sha(repo_dir, c[f"{which}_sha"], SCREEN / key / which, seeded)
        except Exception as e:  # noqa
            res[which] = {"error": repr(e)[-400:]}
    (SCREEN / key / "result.json").write_text(json.dumps(res, indent=1))
    return key, res


seen = set()
todo = []
for c in cands:
    k = (c["repo"], c["pr"])
    if k in seen or not c["fix_sha"] or c["other_warehouse_only"] or not c["code_files"]:
        continue
    if only and f"{c['repo']}-{c['pr']}" not in only:
        continue
    seen.add(k)
    todo.append(c)
SCREEN.mkdir(parents=True, exist_ok=True)
print(len(todo), "to screen", flush=True)
# clone sequentially first, so two threads never clone the same repo
for c in todo:
    if not (REPOS / c["repo"]).exists():
        sh(["git", "clone", "-q", "--filter=blob:none",
            f"https://github.com/fivetran/{c['repo']}.git", str(REPOS / c["repo"])], REPOS)
# one thread per repo so git worktree add does not race in the same repository
by_repo = {}
for c in todo:
    by_repo.setdefault(c["repo"], []).append(c)


def per_repo(cs):
    return [screen(c) for c in cs]


with ThreadPoolExecutor(4) as pool:
    for results in pool.map(per_repo, by_repo.values()):
        for key, res in results:
            line = [key]
            for which in ("parent", "fix"):
                r = res[which]
                if "scenarios" in r:
                    line.append(which + ":" + ";".join(
                        f"{n[:20]}={len(s['failing'])}f/{s['code']}" for n, s in r["scenarios"].items()))
                else:
                    line.append(which + ": SETUP " + str({k: v[:120] for k, v in r.items() if isinstance(v, str)}))
            print(" | ".join(line), flush=True)

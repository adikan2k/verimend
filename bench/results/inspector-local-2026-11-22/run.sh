#!/usr/bin/env bash
# Mimic action/verify's inspect step locally on the demo project, four pull requests.
set -u
WT=${WT:-$(git rev-parse --show-toplevel)}
ROOT=${ROOT:-$HOME/.verimend/inspector-local}
rm -rf "$ROOT"; mkdir -p "$ROOT"
cd "$WT" && uv run python scripts/build_demo.py > "$ROOT/build_demo.log" 2>&1 || { echo "build_demo failed"; exit 1; }
REPO="$ROOT/repo"; mkdir -p "$REPO"
cp -r "$WT/demo/jaffle_shop/." "$REPO/"
rm -rf "$REPO/target" "$REPO/logs"
cd "$REPO" && git init -q -b main && printf 'target/\nlogs/\n*.duckdb\n*.wal\n' > .gitignore && echo "# shop" > README.md && git add -A && git -c user.name=t -c user.email=t@t commit -qm base
BASE=$(git rev-parse HEAD)
PY="uv run --project $WT python"
snap() { rm -rf "$ROOT/work"; cp "$WT/demo/jaffle_shop/warehouse.duckdb" "$REPO/warehouse.duckdb"; (cd "$WT" && $PY -m verifier snapshot --project-dir "$REPO" --work "$ROOT/work" > "$ROOT/snapshot.json") || { echo "snapshot failed"; cat "$ROOT/snapshot.json"; exit 1; }; }
case_run() { # out, branch, extra flags
  out=$1; name=$2; shift 2
  git -C "$REPO" diff --no-color "$BASE" "$name" > "$ROOT/$name.diff"
  (cd "$WT" && $PY -m verifier verify --work "$ROOT/work" --diff "$ROOT/$name.diff" --policy pr "$@" > "$ROOT/$out.json"); code=$?
  (cd "$WT" && $PY -m verifier render --result "$ROOT/$out.json" > "$ROOT/$out.md")
  echo "== $out ($name $*) exit=$code"; python -c "import json,sys;d=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]);print('passed',d['passed'],'failed_check',d['failed_check']);[print('  ',c['name'],c['passed'],c['detail'][:220]) for c in d['checks']]" "$ROOT/$out.json"
}
git -C "$REPO" checkout -qb weaken-test main
python - "$REPO/models/marts/schema.yml" <<'PY'
import sys;p=sys.argv[1];t=open(p).read()
t=t.replace("          - unique\n          - not_null\n","          - unique\n          - not_null:\n              config:\n                severity: warn\n",1);open(p,"w").write(t)
PY
git -C "$REPO" -c user.name=t -c user.email=t@t commit -qam "warn on customer_id"
git -C "$REPO" checkout -qb readme-only main && echo "More words." >> "$REPO/README.md" && git -C "$REPO" -c user.name=t -c user.email=t@t commit -qam readme
git -C "$REPO" checkout -qb real-change main
sed -i 's/^select \* from final/select * from final -- reviewed/' "$REPO/models/marts/orders.sql"
git -C "$REPO" -c user.name=t -c user.email=t@t commit -qam "touch orders" || echo "real-change: nothing changed"
git -C "$REPO" checkout -q main
snap
case_run 1-weaken-test weaken-test
case_run 2-weaken-test-labelled weaken-test --allow-test-changes
case_run 3-readme-only readme-only
case_run 4-real-change real-change

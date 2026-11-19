# pyright: reportMissingImports=false
"""Free check before the paid pilot: imports work in the runner and the draft guards refuse
what they should. Run inside the runner: python /verimend/scripts/spikes/pilot_smoke.py"""

import sys

sys.path.insert(0, "/verimend/scripts/spikes")

import claude_agent_sdk  # noqa: E402
import pilot  # noqa: E402

from verimend.config import load_settings  # noqa: E402

EDITS = {
    "models/a.sql": True,
    "macros/m.sql": True,
    "dbt_project.yml": True,
    "tests/assert_x.sql": False,
    "../etc/passwd": False,
    "/app/models/a.sql": False,
    "models/../tests/x.sql": False,
    "verifier/x.py": False,
}
QUERIES = {
    "select 1": True,
    "with a as (select 1) select * from a": True,
    "  SELECT 2 ;": True,
    "select 1; drop table x": False,
    "drop table x": False,
    "": False,
}

failures = [p for p, want in EDITS.items() if pilot.is_editable(p) is not want]
failures += [q for q, want in QUERIES.items() if pilot.is_single_select(q) is not want]
print("sdk", claude_agent_sdk.__version__)
print("key present:", load_settings().anthropic_api_key is not None)
print("guard failures:", failures or "none")
sys.exit(1 if failures else 0)

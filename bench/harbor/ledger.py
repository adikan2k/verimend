"""The batch budget across Harbor's process boundary.

`harbor run` is a subprocess of bench/harbor/inner.py and runs one trial at a time
(n_concurrent_trials=1), so a small JSON file in the batch's stage folder is enough to
carry what the batch has spent from one trial to the next.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from verimend.hooks.caps import BatchBudget


@dataclass(frozen=True)
class Ledger:
    path: Path
    max_usd: float

    def spent_usd(self) -> float:
        try:
            return float(json.loads(self.path.read_text(encoding="utf-8"))["spent_usd"])
        except FileNotFoundError:
            return 0.0

    def next_run_cap(self, per_run_usd: float) -> float | None:
        """The next run's dollar cap, or None when the batch has no money left."""
        return BatchBudget(self.max_usd, self.spent_usd()).next_run_cap(per_run_usd)

    def record(self, cost_usd: float) -> float:
        budget = BatchBudget(self.max_usd, self.spent_usd())
        budget.record(cost_usd)
        self.path.write_text(json.dumps({"spent_usd": budget.spent_usd}) + "\n", "utf-8")
        return budget.spent_usd

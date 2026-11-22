"""What the verifier allows, by use.

`fix` (the default) judges an agent's fix of a failed run, and is what every benchmark
number was measured with: only dbt resource files may change, and no test may be touched.

`pr` judges any pull request to a dbt project, written by a person or an AI (the inspector
Action, action/verify). A pull request may change its README or CI, delete a model nobody
needs, or add tests, so:

- files outside the dbt project's resources are listed as not checked, never refused;
- a removed, loosened or re-tagged test, or a changed test macro, still fails, naming each
  one, unless a maintainer allows test changes (a label on the pull request);
- a removed or disabled model fails only when it hides a failure: the model, or something
  downstream of it, failed in the base build;
- a pull request with no dbt change passes with nothing to check.

Every other check (masking's filters, the clean rebuild, collateral, row accounting) is the
same under both.
"""

from __future__ import annotations

from dataclasses import dataclass

NAMES = ("fix", "pr")


@dataclass(frozen=True)
class Policy:
    name: str = "fix"
    allow_test_changes: bool = False

    def __post_init__(self) -> None:
        if self.name not in NAMES:
            raise ValueError(f"unknown policy {self.name!r}")
        if self.allow_test_changes and self.name != "pr":
            raise ValueError("test changes can only be allowed under the pr policy")

    @property
    def is_pr(self) -> bool:
        return self.name == "pr"


FIX = Policy()

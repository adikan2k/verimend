<!-- verimend-inspector -->
### VeriMend inspector: passed

| Check | Result | Detail |
| --- | --- | --- |
| Tests and files | pass | 1 changed file(s); 1 outside the dbt project, not checked: README.md |

Checked in 0 s. No model was called.

<details><summary>What this checks, and what it cannot see</summary>

It rebuilds the project from the pull request's base on a fresh copy of the database, applies the change, and checks that no test was removed or weakened, that no failure was hidden, that `dbt build` passes, that models outside the change are unchanged, and that no LEFT join groups unmatched rows under a NULL key. Rows a LEFT join drops are reported, not failed.

It cannot tell a right business rule from a wrong one: a join that builds cleanly and changes no test, but counts the wrong rows, passes. A person still reviews.

</details>

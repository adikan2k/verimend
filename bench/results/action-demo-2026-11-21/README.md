# The Action, end to end (phase 6)

Two deliberately broken commits pushed to `main` of the private demo repository `adikan2k/verimend-demo`, each failing its CI and calling the VeriMend Action (`adikan2k/verimend/action@phase-6/github-action`, model claude-haiku-5-5, cap $0.25). The run records here are the Action's `verimend-run` artifacts.

| Case | Commit | Result | Cost | Steps | Seconds | Record |
| --- | --- | --- | --- | --- | --- | --- |
| Logic bug: orders took `max(amount)` | `3a219f1` "Update order totals" | Fixed and verified; pull request #1 from `verimend/fix-3a219f1` restores `sum(amount)` | $0.0035 | 18 | 54 | [run.json](logic-bug/run.json), [pull request body](logic-bug/pull_request.md), [diff](logic-bug/fix.diff) |
| Bad data: three negative payments | `29a527b` "Load the November payments export" | Escalated, category `bad_data`; diagnosis posted as a commit comment; no branch pushed | $0.0037 | 17 | 31 | [run.json](bad-data/run.json), [diagnosis](bad-data/diagnosis.md) |

Checked by hand after the runs: the fix branch builds all 39 nodes and every model equals the clean demo project built on clean data (the fault grader's answer key). Both runs used `inspect_history` to find the commit that broke the build.

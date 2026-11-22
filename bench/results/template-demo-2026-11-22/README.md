# The inspector's first runs on GitHub

[adikan2k/verimend-inspector-demo](https://github.com/adikan2k/verimend-inspector-demo), built by `scripts/make_demo_repo.py template` (decision 0010), with `adikan2k/verimend/action/verify@verify-v1`. Each pull request's comment is copied here as the inspector posted it. [runs.json](runs.json) has every run, including the fixer workflow on `main`.

| Pull request | Expected | Run | Stamp |
| --- | --- | --- | --- |
| [#1](https://github.com/adikan2k/verimend-inspector-demo/pull/1) `try/weaken-test` | Fail at tests | [failure](https://github.com/adikan2k/verimend-inspector-demo/actions/runs/44984807867) | Failed at tests and files: `not_null_customers_customer_id` changed severity ([comment](pr1-comment.md)) |
| [#2](https://github.com/adikan2k/verimend-inspector-demo/pull/2) `try/null-month` | Fail at row accounting | [failure](https://github.com/adikan2k/verimend-inspector-demo/actions/runs/44984810480) | Failed at row accounting: 5 rows under a NULL `order_month` ([comment](pr2-comment.md)) |
| [#3](https://github.com/adikan2k/verimend-inspector-demo/pull/3) `try/good-change` | Pass | [success](https://github.com/adikan2k/verimend-inspector-demo/actions/runs/44984813145) | Passed all five checks ([comment](pr3-comment.md)) |
| `main` push, `fix.yml` | Build green, no fixer | [success](https://github.com/adikan2k/verimend-inspector-demo/actions/runs/44984796508) | `dbt build` passed and the VeriMend step was skipped. The build was green, so this run does not exercise the key gate; `tests/test_demo_repo.py` checks it in the workflow file |

Each inspection took 3 to 8 seconds once dbt was installed. The repository is private, so the links to its pull requests and runs open only for its owner; the comments are copied here in full.

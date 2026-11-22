# VeriMend inspector demo

A small dbt project on DuckDB (the jaffle shop) with the [VeriMend inspector](https://github.com/adikan2k/verimend/tree/main/action/verify) on every pull request. The inspector rebuilds the project from the pull request's base on a fresh copy of the database, applies the change, and stamps the result on the pull request. It needs no API key and calls no model, so it costs nothing to run.

## See it

Three pull requests here are open on purpose, each with the inspector's comment:

| Pull request | What it does | Stamp |
| --- | --- | --- |
| `try/weaken-test` | Sets the `not_null` test on `customers.customer_id` to `warn`, the quickest way to turn a red build green | Fails at **Tests and files**, naming the test |
| `try/null-month` | Adds monthly revenue without returned orders, but filters the orders it joins instead of the payments. The returned orders' payments land in a month that is NULL | Fails at **Row accounting** |
| `try/good-change` | The same model with an inner join, and a test on the month | Passes |

## Try it

1. **Use this template** (top right) to make your own copy. Pull requests from forks get the result in the job summary but no comment, because a fork's token cannot write; your own copy gets both.
2. Edit a model in the browser, for example `models/marts/orders.sql`, and choose "Create a new branch and start a pull request".
3. The inspector's stamp appears on the pull request in a few minutes.

A pull request that changes or removes a test fails unless it carries the label `verimend:tests-changed`. With the label it passes, and the changed tests are still listed.

## What it cannot see

It does not know your business rules. A change that builds cleanly, touches no test and computes the wrong number passes. A person still reviews the pull request.

## The fixer, if you bring your own key

`.github/workflows/fix.yml` builds the project on every push to `main`. If the build fails and your copy has an `ANTHROPIC_API_KEY` secret, VeriMend tries a fix. It opens a pull request only for a fix its verifier passed; otherwise it comments its diagnosis. Without that secret, which this template does not have, it never calls a model. It never runs on pull requests.

Raw data lives in `raw_data/` and is loaded by `scripts/load_raw.py`. See `NOTICE` for where the project comes from.

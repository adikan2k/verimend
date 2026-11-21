# VeriMend demo

A small dbt project on DuckDB (the jaffle shop) whose CI calls the [VeriMend](https://github.com/adikan2k/verimend) GitHub Action when `dbt build` fails.

Some commits here are broken on purpose, to show what VeriMend does:

- **A logic bug** ("Update order totals"): VeriMend should open a pull request from `verimend/fix-<sha>` that the independent verifier passed.
- **Bad data** ("Load the November payments export"): the models are right and the data is wrong, so VeriMend should not patch anything. It should post its diagnosis as a comment on the commit.

Nothing merges itself. Raw data lives in `raw_data/` and is loaded by `scripts/load_raw.py`. See `NOTICE` for where the project comes from.

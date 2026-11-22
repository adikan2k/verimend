# Notice: Fivetran dbt packages

Each case under `bench/realworld/` comes from one of Fivetran's dbt packages
(`github.com/fivetran/dbt_*`), Copyright © Fivetran Inc., licensed under the Apache
License, Version 2.0. A copy of the licence is in [`LICENSE-fivetran.txt`](LICENSE-fivetran.txt).
None of the packages ships a NOTICE file.

What this folder holds from those packages:

- `<case>/reference/fix.diff`: the change a Fivetran pull request made to the case's
  files, unchanged. `case.json` names the repository, the pull request and both commits.

What `python -m bench.realworld build` does to a package when it builds a case (nothing
of this is committed here; it happens in a folder under `~/.verimend/realworld/`):

- the package at the parent commit becomes the root dbt project: the integration-test
  project's profile, vars, dispatch and flags are merged into its `dbt_project.yml`;
- the packages it installs from dbt Hub are copied under `vendor/` and listed as local
  packages, and their own package files removed, so `dbt deps` needs no network;
- the integration-test project's singular tests and macros move to
  `tests/integration_tests/` and `macros/integration_tests/`;
- `docs/`, CI and release tooling, binary files and the integration-test folder are left
  out, and any CHANGELOG section that points at the fix is removed;
- the seeds are loaded into a DuckDB file instead of shipping as CSV files.

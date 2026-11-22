# The inspector on four pull requests, run locally

`run.sh` makes a scratch git repository from `demo/jaffle_shop`, opens four branches against its `main`, and runs what `action/verify/action.yml`'s inspect step runs: snapshot the base, `git diff` base to branch, `verify --policy pr`, `render`. No model is called. GitHub itself is not involved; the Action's first run on GitHub is the public template's (phase 6e).

| Pull request | Expected | Result | Comment |
| --- | --- | --- | --- |
| `not_null` on `customers.customer_id` set to `warn` | Fail at tests | Fail at tests, naming the test ([json](1-weaken-test.json)) | [md](1-weaken-test.md) |
| The same, with the label `verimend:tests-changed` | Pass, test change listed | Pass, listed ([json](2-weaken-test-labelled.json)) | [md](2-weaken-test-labelled.md) |
| README only | Pass, nothing to check | Pass, README listed as not checked ([json](3-readme-only.json)) | [md](3-readme-only.md) |
| A comment added to `orders.sql` | Pass all five checks | Pass, 39 nodes built ([json](4-real-change.json)) | [md](4-real-change.md) |

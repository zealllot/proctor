# E2E fixture repo contract

PRoctor's e2e suite uses a sibling GitHub repo `proctor-fixtures` with
pre-built PRs covering each category and several known-broken cases.

The repo MUST contain at least these PRs (numbers stable so the
harness can rely on them):

| PR | Categories | Expected outcome |
|---|---|---|
| #1 | frontend only | All pass |
| #2 | api only      | All pass |
| #3 | schema migration | All pass |
| #4 | mixed frontend+api | All pass + e2e-flow item appears |
| #5 | broken on purpose (frontend) | One fail, fixer succeeds |
| #6 | broken on purpose (api)      | One fail, fixer succeeds |
| #7 | unfixable (intentional)      | Fail, fixer returns null, report says human-needed |
| #8 | docs-only                    | All `lint-only` items, no execution |

The harness calls `claude /proctor <PR#>` against each in dry-run mode
(env `PROCTOR_DRY_RUN=1`) and snapshots structured fields.

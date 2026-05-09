# PRoctor GitHub Action

Composite Action that runs `/proctor` against a PR.

## Triggers

```yaml
on:
  pull_request:
    types: [opened, synchronize]   # default: run plan + execute
  pull_request_target:             # for forked PRs (use carefully)
    types: [opened, synchronize]
  issue_comment:                   # for `/proctor run` resume
    types: [created]
```

## Minimal usage

```yaml
jobs:
  proctor:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write              # needed to push fix branches
    steps:
      - uses: zealllot/proctor/github-action@v0
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Approval mode

In the consuming repo's `.pr-test.yml`:

```yaml
require_approval: true
```

When set, the Action posts the test plan as a comment and exits. A
maintainer comments `/proctor run` to resume from the execute stage.

## Run artifacts

Each run uploads `.proctor/runs/<run-id>/` as an Action artifact named
`proctor-run-<pr-number>`. Includes ChangeMap, TestPlan, ApprovedPlan,
TestResults, FixPRRef, and per-item logs.

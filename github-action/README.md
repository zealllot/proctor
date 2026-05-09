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

## Authentication

Provide **one** of the following inputs:

- `anthropic-api-key` — an Anthropic API key from <https://console.anthropic.com>.
- `claude-code-oauth-token` — a long-lived OAuth token obtained by running `claude setup-token` locally. Use this when you have a Claude.ai subscription and want to consume your subscription quota in CI instead of paying per token through the API.

The Action errors fast if neither is provided.

## Minimal usage (API key)

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

## Minimal usage (Claude subscription)

```yaml
jobs:
  proctor:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    steps:
      - uses: zealllot/proctor/github-action@v0
        with:
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
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

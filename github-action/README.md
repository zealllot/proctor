# PRoctor GitHub Action

Composite Action wrapping `/proctor`. Runs PRoctor on every PR and posts a structured report comment.

## Quick Start (manual)

```yaml
# .github/workflows/proctor.yml
name: PRoctor

on:
  pull_request:
    types: [opened, synchronize]
  issue_comment:
    types: [created]

jobs:
  proctor:
    if: github.event_name != 'issue_comment' || contains(github.event.comment.body, '/proctor run')
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    steps:
      - uses: zealllot/proctor/github-action@v0.2.1
        with:
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Plus a `.pr-test.yml` at the repo root and a secret. The fastest path is `claude /proctor-init` — see the [main README](../README.md).

## Authentication

Provide one of:

| Input | Source | Best for |
|---|---|---|
| `claude-code-oauth-token` | `claude setup-token` (browser flow) | Claude.ai subscribers — uses subscription quota |
| `anthropic-api-key` | <https://console.anthropic.com> | Orgs with API billing |

The Action errors fast if neither is provided.

```bash
# Set the secret without leaking it to the conversation:
claude setup-token            # prints token; copy it
pbpaste | gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <owner>/<repo>
echo -n | pbcopy              # clear clipboard
```

## Repo permissions (required for auto-fix)

Auto-fix needs the workflow to push branches and open PRs:

```bash
gh api -X PUT "/repos/<owner>/<repo>/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=true
```

Or via web UI: **Settings → Actions → General → Workflow permissions** → "Read and write" + "Allow GitHub Actions to create and approve pull requests".

Without these, the report comment still posts but fix-PR creation fails with a clear error.

## Approval mode

In the consuming repo's `.pr-test.yml`:

```yaml
require_approval: true
```

Workflow posts the test plan and exits. A maintainer comments `/proctor run` to resume from the execute stage. Commenter must hold write access on the repo.

## What runs in the Action

```
checkout
    ↓
cache Claude Code  (v0.1.13+, ~10s skipped on hit)
    ↓
install Claude (if cache miss)
    ↓
run /proctor pipeline:  analyze → plan → execute (parallel) → fix → report
    ↓
push screenshots → proctor-screenshots branch  (for inline-rendered PR comment images)
    ↓
upload .proctor/runs/ as Action artifact  (proctor-run-<PR>)
```

Per-item execute dispatches at concurrency 3 by default (override via `PROCTOR_EXECUTE_CONCURRENCY` env). 7-item plan runs in ~10–12 minutes wall-clock.

## Run artifacts

Every run uploads `.proctor/runs/<run-id>/` as `proctor-run-<PR>.zip`:

```
<run-id>/
├── pr.json            ← gh pr view --json
├── diff.patch         ← gh pr diff
├── change-map.json    ← analyze output
├── test-plan.json     ← plan output
├── test-results.json  ← execute output (with summary counters)
├── fix-pr-ref.json    ← fix output (number, url, branch — or null)
├── report.md          ← what was posted
├── usage.jsonl        ← per-stage / per-item token usage
├── logs/<id>.log      ← per-test-item executor log
└── screenshots/<id>.png   ← chrome-devtools screenshots
```

Linked from the PR comment header — click "download artifacts" to get everything.

## Speeding up runs

Add toolchain caching steps **before** the PRoctor action:

```yaml
- uses: actions/setup-go@v5      # Go module + build cache
  with: { go-version: "1.22" }

- uses: pnpm/action-setup@v3     # pnpm content-addressable store
  with: { version: 9 }

- uses: actions/setup-python@v5  # pip wheel cache
  with: { python-version: "3.12", cache: pip }

- uses: zealllot/proctor/github-action@v0.2.1
  with:
    claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Saves ~30–90s per warm-cache run.

## Anti-loop

The action skips itself on `fix-*-*` branches and PRs authored by `github-actions[bot]` (PRoctor's own auto-fix outputs). Stops the recursion: fix → analyzed → fails → opens fix-of-fix → ...

## Troubleshooting

See the [Troubleshooting section in INTEGRATION.md](../docs/INTEGRATION.md#troubleshooting) for common failure modes (auth, no-server skips, force-push detection, fix-branch conflicts, rate limits).

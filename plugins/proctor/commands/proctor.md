---
description: Run PRoctor against a GitHub PR — analyze diff, plan tests, confirm with user, execute, fix failures, report.
argument-hint: "<PR-number-or-URL>"
allowed-tools: Bash(gh *), Bash(jq *), Bash(yq *), Bash(python3 *), Bash(git *), Bash(claude *), Read, Edit, Write, Grep, Glob, Task, AskUserQuestion
---

# /proctor

Run the PRoctor test pipeline against a GitHub PR.

## Inputs

- `$ARGUMENTS` — a PR number (e.g. `123`) or full PR URL.
- Optional `.pr-test.yml` at the current repo root.

## Mode detection

- If env var `GITHUB_ACTIONS=true` is set → CI mode.
- Otherwise → local mode.

## Flow

### 1. Pre-flight

Check `gh` is authenticated:

```bash
gh auth status >/dev/null 2>&1 || {
  echo "ERR: gh not authenticated. Run: gh auth login"
  exit 1
}
```

### 2. Parse + fetch PR

```bash
PR_INPUT="${ARGUMENTS}"
PR_DATA="$(python3 -c "
import json, sys
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/..')
from proctor.scripts.pr_fetch import parse_pr_arg, fetch_pr, fetch_diff
arg = parse_pr_arg('$PR_INPUT')
pr = fetch_pr(arg)
diff = fetch_diff(arg)
print(json.dumps({'pr': pr, 'diff': diff}))
")"
```

Persist to `.proctor/runs/<run-id>/{pr.json,diff.patch}` (run-id from
`runlog.make_run_id`).

### 3. Acquire mutex

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/..')
from proctor.scripts.gh_lock import acquire
import json
ok = acquire(pr_number=$PR_NUMBER, repo=None)
sys.exit(0 if ok else 9)
"
```

Exit code 9 → already running, post a comment on the PR
(`gh pr comment $PR_NUMBER --body "PRoctor already running, skipping"`)
and exit 0. Set a Bash trap to release the lock on exit.

### 4. Stage 1 — analyze

Apply skill `analyzing-pr-changes`. Save output to
`.proctor/runs/<run-id>/change-map.json`. Validate with `schema.py`.

### 5. Stage 2 — plan

Apply skill `planning-pr-tests`. Save output to
`.proctor/runs/<run-id>/test-plan.json`. Validate with `schema.py`.

### 6. Approval gate

Local mode: use `AskUserQuestion` to present each test item; the user
unchecks unwanted ones. Save filtered plan as
`.proctor/runs/<run-id>/approved-plan.json`.

CI mode, `require_approval: false`: copy plan → approved-plan, post
plan as a PR comment headed `## PRoctor — about to run`.

CI mode, `require_approval: true`: post the plan as a comment headed
`## PRoctor — awaiting approval`, release the lock, exit 0. The Action
re-runs on the `/proctor run` comment trigger (see github-action/).

### 7. Stage 3 — execute

Apply skill `executing-pr-tests`. Pass:

- approved plan path
- run-id
- logs dir = `.proctor/runs/<run-id>/`
- base_url + per_test_timeout_seconds from `.pr-test.yml`
- PR head_sha (for force-push detection)

Save output to `.proctor/runs/<run-id>/test-results.json`. Validate.

If `aborted` field is set, post a PR comment "PRoctor: run aborted
(<reason>)" and skip Stages 4 + 5.

### 8. Stage 4 — fix

If `test-results.json` has any `fail` items AND `.pr-test.yml.auto_fix`
is true (default), apply skill `fixing-test-failures`. Save output to
`.proctor/runs/<run-id>/fix-pr-ref.json`.

If no failures or `auto_fix: false`, write `null` to that path.

### 9. Stage 5 — report

Apply skill `reporting-pr-test-results`. Pass test-results, fix-pr-ref,
change-map, run-id, PR number. The skill posts the comment itself; the
command just needs to surface success/failure to the user.

### 10. Release mutex (always, including on failure)

Bash trap from step 3 fires `python3 -c "...gh_lock.release(...)"`.

## Logging

Every stage emits `[proctor:<stage>] start ...` and `done ...` via
`runlog.log_line`. Greppable from the Claude Code transcript.

## Detail references

- Stage contracts: `${CLAUDE_PLUGIN_ROOT}/skills/*/SKILL.md`
- Helpers: `${CLAUDE_PLUGIN_ROOT}/scripts/`

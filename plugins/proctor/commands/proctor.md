---
description: Run PRoctor against a GitHub PR — analyze diff, plan tests, confirm with user, execute, fix failures, report.
argument-hint: "<PR-number-or-URL>"
allowed-tools: Bash(gh *), Bash(jq *), Bash(yq *), Bash(python3 *), Bash(git *), Bash(claude *), Read, Edit, Write, Grep, Glob, Task, AskUserQuestion
---

# /proctor

Run the PRoctor test pipeline against a GitHub PR.

## ⚠ Critical: this command runs the WHOLE pipeline non-stop

Stages 1–9 below are a single sequence, NOT a checklist with pause points.

**Your turn ends ONLY when** one of these has happened:
- The reporting skill completed (= terminal success)
- A hard error aborted the run (auth misconfigured, force-push detected, setup-failed)
- The local-mode approval gate's AskUserQuestion is currently displayed and awaiting a user response
- The CI-mode early-exit ran (`require_approval: true` + `mode=ci` → `[proctor] awaiting approval`, exit 0)

**If you wrote a JSON file and validated it but your turn is still going** — you have not finished. The next concrete tool call (AskUserQuestion, dispatching the next skill, etc.) is still owed. Writing the file is half the work; what comes AFTER the file is the other half. Do not stop between them.

**Specifically, after Stage 1 finishes (change-map.json written + validated)** → invoke skill `planning-pr-tests` for Stage 2 with no pause.

**Specifically, after Stage 2 finishes (test-plan.json written + validated AND the planning skill's self-audit lint passed)** → step 6 has FOUR substeps (6a header → 6b table → 6c estimate → 6d AskUserQuestion). All four MUST execute in order. The planning skill itself runs `plan_smells.py --strict` as its final step (v0.3.35+ self-audit) — by the time you reach step 6 the plan is already audited; do NOT re-run plan_smells here, that's the historical v0.3.32/v0.3.33 design which we deprecated in v0.3.38 because the duplicate gate causes the orchestrator AI to stall ("I just ran this lint, why again?").

**Specifically, after the user answers the approval gate** → save approved-plan.json, then invoke skill `executing-pr-tests` with no pause.

**Specifically, after Stage 3 finishes (test-results.json written)** → invoke skill `fixing-test-failures` (or write fix-pr-ref.json=null if nothing to fix), then immediately invoke skill `reporting-pr-test-results`.

If you find yourself emitting a status line ("done", "validated", "10 items planned") and your turn ENDS there — that's a bug. The status line is a log marker, not a stopping point. Continue.

## Inputs

- `$ARGUMENTS` — a PR number (e.g. `123`) or full PR URL, optionally followed by flags.
- Flags (only meaningful in local mode; ignored in CI):
  - `--post-comment` — also post the report as a PR comment (default off in local mode).
  - `--push-fix` — also push the fix branch and open a fix PR (default off in local mode).
- Optional `.proctor/config.yml` at the current repo root.

## Mode detection

Set these env vars in step 0 and propagate to all stage skills:

```
PROCTOR_MODE          = "ci" if GITHUB_ACTIONS=true, else "local"
PROCTOR_POST_COMMENT  = "1" if MODE=ci OR --post-comment, else "0"
PROCTOR_PUSH_FIX      = "1" if MODE=ci OR --push-fix,     else "0"
```

The defaults are deliberately asymmetric: **CI posts and pushes by default; local does neither**. Local invocations are for the developer testing their own PR before review — they don't want each iteration spamming the PR with comments or auto-opening fix PRs from their personal account.

### Dry-run

If env `PROCTOR_DRY_RUN=1` is set:

- Forces `PROCTOR_POST_COMMENT=0` and `PROCTOR_PUSH_FIX=0` regardless of mode/flags. Print what *would* be posted/pushed instead.
- The mutex label is still acquired and released (locking is not output).

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

Persist to `.proctor/runs/<run-id>/{pr.json,diff.patch}`. The run-id comes from `runlog.make_run_id` which is **keyword-only** (v0.3.x signature):

```python
from runlog import make_run_id
from datetime import datetime, timezone
run_id = make_run_id(
    pr_number=pr['number'],
    head_sha=pr['head_sha'],
    started_at_iso=datetime.now(timezone.utc).isoformat(),
)
```

Calling it positionally (`make_run_id(pr['number'])`) will raise `TypeError: make_run_id() takes 0 positional arguments but 1 was given` — that's the signature enforcing kw-only.

### 3. Acquire mutex (CI mode only)

Skip this entire step when `PROCTOR_MODE=local`. The mutex is a coordination point between concurrent CI runs on the same PR; a developer running PRoctor locally on their own machine doesn't conflict with anything. (Two devs running `/proctor:proctor 123` simultaneously is a corner case nobody hits — they'd both produce their own local artifacts and notice each other.)

CI mode:

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

**DO NOT print the ChangeMap JSON to chat.** Not the full object. Not a pretty-printed excerpt. Not the hunks array with summaries. None of it. The JSON file is the artifact; the next stage reads it from disk. The ONLY chat output this step produces is one line:

```
[proctor:analyze] done — <N> hunks, categories: <list>
```

Then **invoke `Skill(planning-pr-tests)` as your next tool call**. Same response, no AskUserQuestion, no "let me verify the file" preamble, no JSON echo, no `cat change-map.json`. Just emit the one-line status and dispatch the next skill.

If you find yourself wanting to "show the user what was analyzed" — STOP. That's the failure mode every prior trace hit: dumping the JSON consumes the AI's context budget, and 3+ minutes of `Cogitated for ...` follows because the model can no longer hold the next-step intent. The user sees the JSON in `.proctor/runs/<run-id>/change-map.json` if they want it; the report at the end summarizes what changed. Mid-pipeline chat is not the place.

**Concretely**: your next assistant turn after writing change-map.json must contain EITHER:
- The one-line status + a `Skill` tool call for `planning-pr-tests`, OR
- An abort if validation failed.

NOT a JSON code block. NOT a hunks summary. NOT a thinking pause. If the previous turn ended without invoking `Skill(planning-pr-tests)`, your CURRENT turn does it now — don't re-validate, don't re-read the file, just dispatch.

### 5. Stage 2 — plan

Apply skill `planning-pr-tests`. Save output to
`.proctor/runs/<run-id>/test-plan.json`. Validate with `schema.py`.

**DO NOT print the test-plan JSON to chat.** Same reason as Stage 1: dumping the JSON consumes context, the model loses the next-step intent, and you'll spend 3+ minutes in `Cogitated for ...` instead of proceeding. The ONLY chat output here is one line:

```
[proctor:plan] done — <N> items planned
```

Then **proceed to the approval gate in this SAME response** — emit the 4 substeps (6a header / 6b table / 6c estimate / 6d AskUserQuestion) without a thinking pause between Stage 2's status line and 6a's header. Do not "verify the plan", do not "let me look at what was generated" — the planning skill already self-audited via plan_smells. Your next assistant turn must contain BOTH the status line AND the approval gate (4 substeps), in one response.

### 6. Approval gate

**FOUR sub-steps.** All four MUST run in this turn, in order, with no stop between them.

The planning skill (Stage 2) already runs `plan_smells.py --strict` as its final self-audit step (v0.3.35+) and only returns to the orchestrator with a clean plan (or after exhausting 2 regen attempts and logging the residual warnings to `.proctor/runs/<run-id>/plan-smells.txt`). **Do NOT** re-run plan_smells here. The v0.3.32/v0.3.33 "hard-gate at step 6d" design was deprecated in v0.3.38 because the duplicate gate caused the orchestrator AI to stall ("I already ran this lint as the last step of the planning skill — why am I running it again?"). Trust the skill's self-audit.

**6a.** Emit a markdown header line: `## Plan for PR #<num> — <total> items` (as part of your assistant message — do NOT use the Write tool for this; it goes to chat).

**6b.** Emit the plan items as a markdown table (in the SAME assistant message, immediately below 6a's header). Format below. Every column populated for every row.

**6c.** Emit one summary line below the table: `Estimated: ~<N> min, ~$<cost>`. Best-effort estimate (rough: lint-only ≈ 5s/$0.001, bash ≈ 30s/$0.005, chrome-devtools ≈ 60s/$0.05 per item).

**6c-warn** (rare path, v0.3.38+): if `.proctor/runs/<run-id>/plan-smells.txt` exists AND is non-empty (the planning skill exhausted its 2 regen attempts and surfaced residual warnings instead of regen'ing more), render those warnings as a `### Plan smells (still present after 2 regen attempts)` section immediately below 6c. Each warning is a bullet starting with `⚠`. This tells the human reviewer that the planning skill couldn't fix the issue itself — they should choose "Cancel — let me edit the plan first" at 6d. If the file is absent or empty, render nothing.

**6d.** Call AskUserQuestion with exactly THREE options (see below). Do not skip the AskUserQuestion call — that IS the gate; without it, the run is stuck.

**Do NOT** skip the table (6a–6c) and jump straight to AskUserQuestion (6d) — the question is unanswerable without context. **Do NOT** re-run `plan_smells.py` at this stage; the planning skill already did. **Do NOT** print the test-plan JSON instead of the table. **Do NOT** collapse to "3 lint + 5 ui — run?".

Required format for the table:

```markdown
## Plan for PR #<num> — <total> items

| # | Cat | Risk | Tool | As | What |
|---|---|---|---|---|---|
| t-001 | api | low | lint-only | dev | <one-sentence summary of item 1's what:> |
| t-002 | api | low | lint-only | dev | <one-sentence summary of item 2's what:> |
| t-003 | api | medium | bash | dev | <one-sentence summary of item 3's what:> |
| t-004 | api | high | chrome-devtools | dev | <one-sentence summary of item 4's what:> |
| t-005 | api | high | chrome-devtools | dev | <one-sentence summary of item 5's what:> |
| ... |
```

Render EVERY item's `id` / `category` / `risk` / `tool` / `as_account` (or "—" if unset) / a *concise* version of `what` (one sentence; if the original is longer, summarize — but keep accuracy). Don't truncate; let it wrap.

Below the table, render the cost / time estimate if you can compute one (rough: lint-only ≈ 5s each, bash ≈ 30s, chrome-devtools ≈ 60s each; total time is one line, dollar cost is `~$0.05 × runtime_items`).

THEN call AskUserQuestion with simple, decisive options:
- **Run all <N> items** (Recommended)
- **Drop specific items** — opens a follow-up free-text question for the IDs to skip (`t-002 t-007`)
- **Cancel — let me edit the plan first** — abort run; user can hand-edit `.proctor/runs/<run-id>/test-plan.json` and re-invoke `/proctor:proctor`

Save filtered plan as
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
- base_url + per_test_timeout_seconds from `.proctor/config.yml`
- PR head_sha (for force-push detection)

Save output to `.proctor/runs/<run-id>/test-results.json`. Validate.

If `aborted` field is set, post a PR comment "PRoctor: run aborted
(<reason>)" and skip Stages 4 + 5.

**Otherwise immediately proceed to Stage 4.** Don't pause to summarize
pass/fail counts in chat.

### 8. Stage 4 — fix

If `test-results.json` has any `fail` items AND `.proctor/config.yml.auto_fix`
is true (default), apply skill `fixing-test-failures`. Save output to
`.proctor/runs/<run-id>/fix-pr-ref.json`.

If no failures or `auto_fix: false`, write `null` to that path.

**Then immediately proceed to Stage 5.**

### 9. Stage 5 — report

Apply skill `reporting-pr-test-results`. Pass test-results, fix-pr-ref,
change-map, run-id, PR number. The skill posts the comment itself; the
command just needs to surface success/failure to the user.

This is the terminal stage. After report completes (and step 10 releases
the mutex in CI mode), the command finishes naturally.

### 10. Release mutex (CI mode only, always — including on failure)

Bash trap from step 3 fires `python3 -c "...gh_lock.release(...)"`. Skip when `PROCTOR_MODE=local` (no lock was acquired).

## Logging

Every stage emits `[proctor:<stage>] start ...` and `done ...` via
`runlog.log_line`. Greppable from the Claude Code transcript.

## Detail references

- Stage contracts: `${CLAUDE_PLUGIN_ROOT}/skills/*/SKILL.md`
- Helpers: `${CLAUDE_PLUGIN_ROOT}/scripts/`

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

**Specifically, after Stage 2 finishes (test-plan.json written + validated)** → step 6 has **FIVE** substeps (6a header → 6b table → 6c estimate → **6d hard-gate lint** → 6e AskUserQuestion). All five MUST execute in order. The lint at 6d is mandatory; the historical naming `6c-lint` is gone in v0.3.33 — if you remember a "4-substep" sequence from a stale memory, that's wrong, the count is 5. Skipping 6d means the hard gate never fires and the planner ships unaudited plans straight to the human; v0.3.32 added this exact safety net because the planner has demonstrated it cannot reliably self-audit.

**Specifically, after the user answers the approval gate** → save approved-plan.json, then invoke skill `executing-pr-tests` with no pause.

**Specifically, after Stage 3 finishes (test-results.json written)** → invoke skill `fixing-test-failures` (or write fix-pr-ref.json=null if nothing to fix), then immediately invoke skill `reporting-pr-test-results`.

If you find yourself emitting a status line ("done", "validated", "10 items planned") and your turn ENDS there — that's a bug. The status line is a log marker, not a stopping point. Continue.

## Inputs

- `$ARGUMENTS` — a PR number (e.g. `123`) or full PR URL, optionally followed by flags.
- Flags (only meaningful in local mode; ignored in CI):
  - `--post-comment` — also post the report as a PR comment (default off in local mode).
  - `--push-fix` — also push the fix branch and open a fix PR (default off in local mode).
- Optional `.pr-test.yml` at the current repo root.

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

**DO NOT print the ChangeMap JSON to chat.** The JSON file is the
artifact; chat is for humans. The ONLY chat output this step produces
is a one-line status:

```
[proctor:analyze] done — <N> hunks, categories: <list>
```

**Then immediately proceed to Stage 2.**

### 5. Stage 2 — plan

Apply skill `planning-pr-tests`. Save output to
`.proctor/runs/<run-id>/test-plan.json`. Validate with `schema.py`.

**DO NOT print the test-plan JSON to chat.** Same reason as Stage 1 —
JSON is for the validator + the next stage, not a wall of text for
the human. The ONLY chat output here is:

```
[proctor:plan] done — <N> items planned
```

**Then immediately proceed to the approval gate, where the plan gets
rendered as a human-readable table.**

### 6. Approval gate

**FIVE sub-steps.** All five MUST run in this turn, in order, with no stop between them. The substeps are explicitly numbered 6a / 6b / 6c / 6d / 6e — if you remember a "four-substep" version from training data or stale context, IGNORE IT. v0.3.33 inserted 6d (hard-gate lint) as a peer to the others, not as an aside.

**6a.** Emit a markdown header line: `## Plan for PR #<num> — <total> items` (as part of your assistant message — do NOT use the Write tool for this; it goes to chat).

**6b.** Emit the plan items as a markdown table (in the SAME assistant message, immediately below 6a's header). Format below. Every column populated for every row.

**6c.** Emit one summary line below the table: `Estimated: ~<N> min, ~$<cost>`. Best-effort estimate (rough: lint-only ≈ 5s/$0.001, bash ≈ 30s/$0.005, chrome-devtools ≈ 60s/$0.05 per item).

**6d. HARD-GATE LINT** (mandatory; not optional; not advisory). Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/plan_smells.py --strict \
    < .proctor/runs/<run-id>/test-plan.json
echo "EXIT=$?"
```

This is a separate Bash invocation. You MUST run it. You cannot proceed to 6e without running it. The v0.3.32 release added this exact safety net because the planner has demonstrated it ships plans with structural defects (combined happy+negative items, no round-trip sibling for save actions) that the human can't catch by skimming the table — only the lint catches them mechanically.

Behavior based on exit code:

- **Exit 0** (no warnings) → proceed to 6e. Render nothing — the gate is invisible in the happy path.
- **Exit 1** (warnings fired) → DO NOT proceed to 6e. Hard-gate behavior:
  1. Read `.proctor/runs/<run-id>/regen-count.txt` (treat missing as 0). If count < 2, regenerate:
     a. Write the warnings to `.proctor/runs/<run-id>/plan-smells.txt`.
     b. Increment regen-count and write it back.
     c. Print one chat line: `[proctor:plan] hard-gate triggered (attempt <N+1>/3); regenerating plan with smells feedback.`
     d. Re-invoke the `planning-pr-tests` skill with the existing change-map.json AND the smells warnings as feedback — explicitly instruct the planner to:
        - Split every item whose `what:` combines happy and negative phrasing into separate items per assertion class.
        - For every chrome-devtools save/create/update action, add a sibling item linked by `data_from: [<save_id>]` whose `what:` describes re-opening the saved record (use `re-open`, `round-trip`, `reload`, `appears in list`, or `detail page` so the lint recognizes it).
        - Preserve the existing journeys structure; do not start from scratch.
     e. Overwrite `.proctor/runs/<run-id>/test-plan.json` with the regenerated plan.
     f. Re-validate via `schema.py`.
     g. Loop back to 6d (run plan_smells again with the new plan).
  2. If regen-count reached 2 (= the 3rd attempt still failed), STOP regenerating. Fall through to advisory mode for this run only:
     - Emit a `### Plan smells (still present after 2 regeneration attempts)` section with warnings as bullets starting with `⚠`.
     - Print: `[proctor:plan] hard-gate exhausted regen attempts; surfacing warnings to the human reviewer.`
     - Proceed to 6e so the user can pick "Cancel — let me edit the plan first" and intervene manually.

**6e.** Call AskUserQuestion with exactly THREE options (see below). Do not skip the AskUserQuestion call — that IS the gate; without it, the run is stuck.

**Do NOT** skip the table (6a–6c) and jump straight to AskUserQuestion (6e) — the question is unanswerable without context. **Do NOT** skip 6d — that's the mandatory hard-gate; running 6a → 6b → 6c → 6e bypasses the v0.3.32 safety net and ships unaudited plans to the human. **Do NOT** print the test-plan JSON instead of the table. **Do NOT** collapse to "3 lint + 5 ui — run?".

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
- base_url + per_test_timeout_seconds from `.pr-test.yml`
- PR head_sha (for force-push detection)

Save output to `.proctor/runs/<run-id>/test-results.json`. Validate.

If `aborted` field is set, post a PR comment "PRoctor: run aborted
(<reason>)" and skip Stages 4 + 5.

**Otherwise immediately proceed to Stage 4.** Don't pause to summarize
pass/fail counts in chat.

### 8. Stage 4 — fix

If `test-results.json` has any `fail` items AND `.pr-test.yml.auto_fix`
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

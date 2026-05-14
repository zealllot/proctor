---
description: Run PRoctor against a GitHub PR — analyze diff, plan tests, confirm with user, execute, fix failures, report.
argument-hint: "<PR-number-or-URL>"
allowed-tools: Bash(gh *), Bash(jq *), Bash(yq *), Bash(python3 *), Bash(git *), Bash(claude *), Read, Edit, Write, Grep, Glob, Task, AskUserQuestion
---

# /proctor

Run the PRoctor test pipeline against a GitHub PR.

## ⚠ CRITICAL (v0.6.1+): if you stall mid-pipeline, use `/proctor-drive` instead

Real-world v0.6.0 trace: even with the state machine, the main Claude Code AI still stalled after `dispatch_skill` returned (the Skill completed, but the AI ended its turn instead of immediately re-invoking `proctor_run.py`). This is a platform-level turn-model constraint, not a state machine bug — a subagent runs the same pipeline through end-to-end without issue.

**If you find yourself reading this comment because you ALREADY stalled** (the AI is sitting at an empty `❯` prompt mid-pipeline and you typed something to unstick it), the right fix is: kill this session and run `/proctor-drive <PR>` instead. That command dispatches the whole pipeline as one Agent task — no turn boundaries between stages, no stalls.

If you're starting fresh, prefer `/proctor-drive` from the start. This command (`/proctor:proctor`) still works but requires loop discipline the AI doesn't reliably exhibit.

## ⚠ The pipeline is a state-machine loop (v0.6.0+)

**Stop conditions** (the only legitimate ones to end the turn):
- Envelope type is `done` → emit summary, exit loop.
- Envelope type is `error` → emit error, exit loop.
- An `ask_user` envelope's AskUserQuestion is currently displayed and awaiting a user response.

**If you complete any single step and your turn ends without re-invoking `proctor_run.py`** — that's the chronic stall pattern. Don't do it. Iterate.

## Pipeline loop

### 0. Pre-flight (ONCE at the start)

```bash
gh auth status >/dev/null 2>&1 || { echo "ERR: gh not authenticated. Run: gh auth login"; exit 1; }

# Mode detection
export PROCTOR_MODE="${GITHUB_ACTIONS:+ci}"
export PROCTOR_MODE="${PROCTOR_MODE:-local}"

# State-file path lives under .proctor/runs/<run-id>/ once we know the
# run-id. Until then we use a temp path that gets moved after pre-flight
# returns the run-id via the first bash envelope's stdout.
export STATE_FILE="/tmp/proctor-pipeline-state-$$.json"
```

### 1. Loop body — each iteration is ONE assistant turn

**1a. Invoke the state machine:**

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/proctor_run.py \
    --state-file "$STATE_FILE" \
    --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
    --mode "$PROCTOR_MODE" \
    ${PR_ARG:+--pr-arg "$PR_ARG"} \
    ${PREV_ANSWER:+--answer "$PREV_ANSWER"} \
    ${PREV_BASH_RC:+--bash-rc "$PREV_BASH_RC"}
```

On the FIRST invocation pass `--pr-arg "$ARGUMENTS"`. Thereafter omit it. After the pre-flight bash returns RUN_ID/RUN_DIR/PR_NUMBER, parse those from the bash output and update the state file:

```bash
# After the FIRST bash envelope (pre-flight) returns:
RUN_ID=$(echo "$BASH_OUTPUT" | grep '^RUN_ID=' | cut -d= -f2)
RUN_DIR=$(echo "$BASH_OUTPUT" | grep '^RUN_DIR=' | cut -d= -f2)
PR_NUMBER=$(echo "$BASH_OUTPUT" | grep '^PR_NUMBER=' | cut -d= -f2)

# Move state file under the run dir + patch it
NEW_STATE_FILE="$RUN_DIR/pipeline-state.json"
mv "$STATE_FILE" "$NEW_STATE_FILE"
export STATE_FILE="$NEW_STATE_FILE"
python3 -c "
import json
s = json.load(open('$STATE_FILE'))
s['run_id'] = '$RUN_ID'
s['run_dir'] = '$RUN_DIR'
s['pr_number'] = int('$PR_NUMBER')
json.dump(s, open('$STATE_FILE', 'w'), indent=2)
"
```

(Do this update INLINE, in the same response as the pre-flight bash call. Don't pause to "think about" what to do next.)

**1b. Branch on envelope type** — exactly one action per iteration:

- **`type=ask_user`**: call `AskUserQuestion` with the `header` / `question` / `options`. Save the user's selection as `PREV_ANSWER=<label>`. **Continue in the same response** — re-invoke proctor_run.py with `--answer "$PREV_ANSWER"`.

- **`type=show`**: emit the `markdown` field verbatim to chat. Save `PREV_ANSWER=` and `PREV_BASH_RC=` (clear both). **Continue in the same response** — re-invoke.

- **`type=bash`**: run the `command` field via Bash. Save `PREV_BASH_RC=<exit-code>`. Save `PREV_ANSWER=`. **Continue in the same response** — re-invoke.

- **`type=dispatch_skill`**: invoke the `skill` name via the Skill tool. The skill writes its artifact to `expects_artifact` (the script will validate it on next invocation). **Continue in the same response** — re-invoke (no flags).

- **`type=done`**: emit the `summary` field. Exit the loop. End the turn.

- **`type=error`**: emit the `message` field. Exit the loop with the error.

### 2. Loop discipline (anti-stall checklist)

- Every iteration ends with re-invoking proctor_run.py UNLESS the envelope is `done`/`error` or an AskUserQuestion is displayed and awaiting answer.
- Don't dump artifact JSON between iterations. Don't summarize what the previous stage did. Just dispatch the next action.
- Skills (Stage 1-5) handle their own work — the orchestrator's job is to TELL them to run, not to think about them.

### 3. What the state machine handles internally

- Stage 1 (analyze): emits `dispatch_skill` for `proctor:analyzing-pr-changes`, validates change-map.json on next invocation.
- Stage 2 (plan): emits `dispatch_skill` for `proctor:planning-pr-tests`, validates test-plan.json.
- Approval gate: emits `bash` for `render_plan_table.py`, then `ask_user` with 2 options. "Run all" copies plan → approved-plan + dispatches execute. "Cancel" emits done.
- Stage 3 (execute): emits `dispatch_skill` for `proctor:executing-pr-tests`, validates test-results.json.
- Stage 4 (fix): conditionally emits `dispatch_skill` for `proctor:fixing-test-failures` when fail_count > 0; otherwise writes `fix-pr-ref.json = null` and skips.
- Stage 5 (report): emits `dispatch_skill` for `proctor:reporting-pr-test-results`.
- Final: emits `done` with the file:// URL to report.html.

---

## Legacy prose (fallback documentation)

The sections below are the v0.3.x / v0.4.x prose-driven version of the pipeline. v0.6.0 superseded them with the state machine above. They're kept for reference and as fallback for any flow the state machine doesn't yet handle (CI mode's `require_approval=true` early-exit, mutex acquire, etc.).

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

**TWO tool calls.** That's the whole gate (v0.4.3+).

The v0.4.0-and-earlier design had the AI hand-render the markdown table (header + 13-row table + estimate) in chat, then call AskUserQuestion. That hand-rendering proved unreliable across SIX releases of prose-tightening — the AI kept dumping the test-plan JSON to chat before/instead of the table, consuming context and stalling for 3-5 minutes. v0.4.3 takes the render out of the AI's hands.

**6a — Bash**: run the deterministic renderer. Its stdout goes straight to chat:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_plan_table.py \
    --pr-number <num> \
    --run-dir .proctor/runs/<run-id> \
    < .proctor/runs/<run-id>/test-plan.json
```

The script outputs:
- `## Plan for PR #<num> — <total> items`
- The markdown table (`| # | Cat | Risk | Tool | As | What |` — one row per item).
- An `**Estimated:**` line summing per-tool seconds + dollars.
- IF `plan-smells.txt` exists and is non-empty (the planning skill exhausted its 2 regen attempts), a `### Plan smells (still present after 2 regen attempts)` section with each warning as a `⚠` bullet.

You do NOT format anything yourself. No prefacing text. No JSON. No "let me show you the plan" wrapper. The bash output IS the chat content for the approval gate.

**6b — AskUserQuestion**: exactly three options (see below). Same response as 6a — no thinking pause between them.

The planning skill (Stage 2) already ran `plan_smells.py --strict` as its self-audit (v0.3.35+) and only returned with a clean plan or a `plan-smells.txt` residual file. **Do NOT** re-run plan_smells here — the renderer surfaces residuals.

**Do NOT** skip 6a and jump to AskUserQuestion. **Do NOT** print the test-plan JSON before 6a. **Do NOT** hand-render the table — the renderer does it deterministically. **Do NOT** insert chat text between 6a's bash output and 6b's AskUserQuestion.

Required AskUserQuestion options:
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

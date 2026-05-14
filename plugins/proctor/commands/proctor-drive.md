---
description: Run /proctor:proctor end-to-end via a subagent, bypassing main-session inter-stage stalls. The subagent has the same tools but runs the whole pipeline as one task — no turn-boundary stops between stages.
argument-hint: "<PR-number-or-URL>"
allowed-tools: Task, AskUserQuestion
---

# /proctor-drive

Run the PRoctor pipeline against a GitHub PR as a single subagent task.

## ⚠ Why this command exists

v0.5.0 / v0.6.0 moved the wizard + pipeline control flow into Python state machines. The state machines work correctly (subagent acceptance tests pass end-to-end). What still fails in the main Claude Code session: the AI's *loop discipline*. After each `dispatch_skill` envelope's Skill returns, the AI tends to end its turn instead of re-invoking `proctor_run.py` for the next iteration. Result: 3-5 minute stalls between stages, "继续" prompts needed, can't be automated for CI.

The reason is a platform constraint: Claude Code's turn model lets the AI end a turn after any tool call. Prose-tightening can't structurally prevent this — the AI's "completion" signal fires too easily.

**Subagents don't have this problem.** They run end-to-end as one task; there's no concept of an inter-step user prompt. The subagent we use for acceptance testing successfully drives the pipeline through 9 iterations / 5 stages every time.

This command makes the subagent workflow the user's primary entry point.

## What it does

1. Capture `$ARGUMENTS` (the PR number/URL).
2. Dispatch a `general-purpose` Agent with a prompt that contains the full v0.6.0 harness loop instructions verbatim + the PR argument.
3. The subagent runs `scripts/proctor_run.py` in a loop, dispatching each envelope's action (`bash` / `dispatch_skill` / `show` / `ask_user` / `done` / `error`) until `done`.
4. When the subagent returns, the main session emits the final report path to chat and exits.

For local mode: the subagent dispatches actual Skill invocations. For the approval gate's `ask_user`, the subagent calls `AskUserQuestion`. Same UX as `/proctor:proctor` but with no inter-stage stall risk.

## Procedure (single Agent dispatch)

Dispatch ONE subagent. Its prompt should include:

```
## Mission

Run PRoctor v0.6.0 pipeline against PR <ARGUMENTS>. Iterate the
state machine in scripts/proctor_run.py until `done` or `error`.
Each iteration: invoke script → parse envelope → handle one action
→ re-invoke. NEVER stop except on done/error/awaiting-AskUserQuestion.

## Setup

cd <consumer repo root>
export PROCTOR_MODE=local      # or ci based on $GITHUB_ACTIONS
export STATE_FILE=/tmp/proctor-pipeline-state-$$.json
export CLAUDE_PLUGIN_ROOT=<plugin root>

## First iteration

python3 ${CLAUDE_PLUGIN_ROOT}/scripts/proctor_run.py \
    --state-file "$STATE_FILE" \
    --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
    --mode "$PROCTOR_MODE" \
    --pr-arg "<ARGUMENTS>"

## Envelope handling

(see commands/proctor.md harness — same loop, same envelope types)

## Stop conditions

ONLY done / error / displayed-and-awaiting-AskUserQuestion.

## Report

Final state-machine `done` summary + report.html path.
```

After Agent returns, emit its result to chat. End.

## CI compatibility

In CI mode (`$GITHUB_ACTIONS=true`), this command falls back to the
existing `/proctor:proctor` flow via the workflow's action (CI doesn't
go through the Claude Code session, so the turn-model stall doesn't
apply there). This command is primarily for **local** developer use
where the stall is the actual blocker.

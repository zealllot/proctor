#!/bin/bash
# PRoctor Stop hook — auto-continue when /proctor:proctor pipeline is
# mid-flight (v0.7.4+).
#
# Why this exists
# ---------------
# The /proctor:proctor pipeline is a state-machine loop. proctor_run.py
# advances one stage per AI turn (each invocation = one transition,
# emits exactly one envelope, exits). For the loop to complete, the AI
# must immediately re-invoke proctor_run.py after handling each envelope.
#
# Claude Code's platform turn-model lets the AI end its turn naturally
# after any tool call. v0.6.x tried tightening the slash-command prose
# ("DO NOT END YOUR TURN MID-LOOP") — real runs showed it doesn't stick.
# Users have to type "继续" / "continue" between every stage, which makes
# the pipeline unusable as an automated CI flow and tedious locally.
#
# How this works
# --------------
# Hook fires on every assistant turn-stop. We:
#  1. Look for `.proctor/runs/*/pipeline-state.json` in $CLAUDE_PROJECT_DIR
#     (set by Claude Code) or $PWD.
#  2. Pick the most-recent one by mtime.
#  3. Read `step`. If "done" → allow stop (pipeline is finished).
#  4. If mtime > 5 minutes old → allow stop (pipeline is abandoned;
#     don't trap the user in a dead session).
#  5. Otherwise: exit 2 + stderr telling the AI to re-invoke
#     proctor_run.py. Claude Code treats exit 2 as "block stop" and
#     feeds stderr to the AI as a continuation prompt.
#
# Stale-pipeline safety
# ---------------------
# proctor_run.py touches `pipeline-state.json` on every iteration, so an
# active pipeline always has a fresh mtime. The 5-minute window lets a
# legitimately-stuck run (chrome hang, server slow) keep the loop alive,
# but a session the user walked away from eventually frees up. The
# `step == "done"` short-circuit handles clean completion.

set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
RUNS_DIR="$PROJECT_DIR/.proctor/runs"

# No PRoctor in this project → no-op.
if [[ ! -d "$RUNS_DIR" ]]; then
    exit 0
fi

# Read stdin (Claude Code passes hook event JSON; we don't need it, but
# we must drain to avoid SIGPIPE on the sender side).
cat > /dev/null

# Find most-recent pipeline-state.json across all run dirs. `ls -t`
# sorts by mtime descending; head -1 takes the newest.
STATE_FILE=$(ls -t "$RUNS_DIR"/*/pipeline-state.json 2>/dev/null | head -1 || true)
if [[ -z "$STATE_FILE" || ! -f "$STATE_FILE" ]]; then
    exit 0
fi

# Cross-platform mtime (BSD stat on macOS uses -f, GNU stat uses -c).
if mtime=$(stat -f %m "$STATE_FILE" 2>/dev/null); then
    :
else
    mtime=$(stat -c %Y "$STATE_FILE")
fi
now=$(date +%s)
age=$(( now - mtime ))

# Abandoned pipeline (> 5 min since last state-file write) → allow stop.
if (( age > 300 )); then
    exit 0
fi

# Parse `step` via python3 (already a hard requirement for the plugin).
STEP=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get('step', ''))
except Exception:
    print('')
" "$STATE_FILE" 2>/dev/null || echo "")

# Empty step or terminal step → allow stop.
if [[ -z "$STEP" || "$STEP" == "done" ]]; then
    exit 0
fi

# Mid-flight pipeline. Block stop + tell AI exactly what to run next.
# The AI sees stderr as a user message and continues the turn.
RUN_DIR=$(dirname "$STATE_FILE")
cat >&2 <<EOF
PRoctor pipeline is mid-flight — DO NOT end this turn.

State file: $STATE_FILE
Current step: $STEP
Run dir: $RUN_DIR

Re-invoke the state machine to advance to the next stage:

  python3 "\${CLAUDE_PLUGIN_ROOT}/scripts/proctor_run.py" \\
      --state-file "$STATE_FILE" \\
      --plugin-root "\${CLAUDE_PLUGIN_ROOT}" \\
      --mode "\${PROCTOR_MODE:-local}"

Then handle whatever envelope it emits (bash / dispatch_skill / ask_user / show / done / error). Keep iterating until the envelope type is \`done\` or \`error\`.
EOF
exit 2

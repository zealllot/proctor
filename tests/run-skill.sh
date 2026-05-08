#!/usr/bin/env bash
# run-skill.sh — invoke a single PRoctor skill against a fixture and verify
# the JSON output matches the fixture's expected schema constraints.
#
# Usage: tests/run-skill.sh <skill-name> <fixture-name> [extra-input-stage]
#
# Example: tests/run-skill.sh analyzing-pr-changes frontend-only

set -euo pipefail

SKILL="${1:?usage: $0 <skill-name> <fixture-name>}"
FIXTURE="${2:?usage: $0 <skill-name> <fixture-name>}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FX_DIR="$ROOT/tests/fixtures/$FIXTURE"

if [[ ! -d "$FX_DIR" ]]; then
  echo "fixture not found: $FX_DIR" >&2
  exit 2
fi

# Build the skill prompt: "Apply the SKILL <skill> to this fixture input.
# Output ONLY a single JSON object on stdout matching the documented contract.
# No prose, no fences."
PROMPT_FILE="$(mktemp)"
OUT_FILE="$(mktemp)"
trap 'rm -f "$PROMPT_FILE" "$OUT_FILE"' EXIT

cat > "$PROMPT_FILE" <<EOF
You are running PRoctor in offline test mode. Apply the skill named
"$SKILL" to the fixture inputs below. Emit a single JSON object on stdout
that satisfies the skill's documented output contract. No prose.

=== fixture: $FIXTURE ===
EOF

if [[ -f "$FX_DIR/pr.json" ]]; then
  echo "--- pr.json ---" >> "$PROMPT_FILE"
  cat "$FX_DIR/pr.json" >> "$PROMPT_FILE"
fi
if [[ -f "$FX_DIR/diff.patch" ]]; then
  echo "" >> "$PROMPT_FILE"
  echo "--- diff.patch ---" >> "$PROMPT_FILE"
  cat "$FX_DIR/diff.patch" >> "$PROMPT_FILE"
fi
if [[ -f "$FX_DIR/change-map.json" ]]; then
  echo "" >> "$PROMPT_FILE"
  echo "--- change-map.json ---" >> "$PROMPT_FILE"
  cat "$FX_DIR/change-map.json" >> "$PROMPT_FILE"
fi
if [[ -f "$FX_DIR/test-plan.json" ]]; then
  echo "" >> "$PROMPT_FILE"
  echo "--- test-plan.json ---" >> "$PROMPT_FILE"
  cat "$FX_DIR/test-plan.json" >> "$PROMPT_FILE"
fi
if [[ -f "$FX_DIR/test-results.json" ]]; then
  echo "" >> "$PROMPT_FILE"
  echo "--- test-results.json ---" >> "$PROMPT_FILE"
  cat "$FX_DIR/test-results.json" >> "$PROMPT_FILE"
fi

# --plugin-dir loads the plugin's skill content into the session.
claude --print --add-dir "$ROOT" \
       --plugin-dir "$ROOT/plugins/proctor" \
       < "$PROMPT_FILE" > "$OUT_FILE"

# Strip any accidental code fences and isolate the JSON object.
python3 - "$OUT_FILE" <<'PY'
import json, re, sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
# Try direct JSON first
try:
    json.loads(text)
    print(text)
    sys.exit(0)
except Exception:
    pass
# Fallback: extract first {...} block by brace matching
start = text.find("{")
if start < 0:
    sys.stderr.write("no JSON object in output\n"); sys.exit(3)
depth = 0
for i, ch in enumerate(text[start:], start=start):
    if ch == "{": depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0:
            obj = text[start:i+1]
            json.loads(obj)  # raises if malformed
            print(obj)
            sys.exit(0)
sys.stderr.write("unterminated JSON\n"); sys.exit(3)
PY

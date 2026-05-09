#!/usr/bin/env bash
# run-e2e.sh — run /proctor in dry-run mode against the proctor-fixtures
# sibling repo and assert the structured result for each known PR.
#
# Requires: gh authenticated, ANTHROPIC_API_KEY set, the
# proctor-fixtures repo present at $PROCTOR_FIXTURES (default:
# ../proctor-fixtures relative to this repo).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FX_REPO="${PROCTOR_FIXTURES:-$ROOT/../proctor-fixtures}"

if [[ ! -d "$FX_REPO/.git" ]]; then
  echo "Fixture repo not found at $FX_REPO. See tests/fixtures/E2E.md" >&2
  exit 2
fi

# Each line: <pr#>:<expected-status>:<expected-fix-pr>
declare -a CASES=(
  "1:all-pass:none"
  "2:all-pass:none"
  "3:all-pass:none"
  "4:all-pass:none"
  "5:one-fail:opened"
  "6:one-fail:opened"
  "7:one-fail:none-unfixed"
  "8:all-skipped:none"
)

PASS=0; FAIL=0
for case in "${CASES[@]}"; do
  IFS=":" read -r PR EXPECT_STATUS EXPECT_FIX <<<"$case"
  echo "==> PR #$PR (expect: status=$EXPECT_STATUS fix=$EXPECT_FIX)"
  pushd "$FX_REPO" >/dev/null

  PROCTOR_DRY_RUN=1 \
    claude --plugin-dir "$ROOT/plugins/proctor" /proctor "$PR" \
    > "/tmp/proctor-e2e-$PR.log" 2>&1 || true

  # Pull structured outputs from the latest run dir
  RUN_DIR="$(ls -td .proctor/runs/* | head -1)"
  TR="$RUN_DIR/test-results.json"
  FX="$RUN_DIR/fix-pr-ref.json"

  python3 - "$TR" "$FX" "$EXPECT_STATUS" "$EXPECT_FIX" <<'PY' && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
import json, sys, pathlib
tr = json.load(open(sys.argv[1]))
fx_path = pathlib.Path(sys.argv[2])
fx = json.loads(fx_path.read_text()) if fx_path.exists() and fx_path.read_text().strip() != "null" else None
expect_status, expect_fix = sys.argv[3], sys.argv[4]
ok = True
if expect_status == "all-pass":
    ok &= tr["summary"]["fail"] == 0
elif expect_status == "one-fail":
    ok &= tr["summary"]["fail"] == 1
elif expect_status == "all-skipped":
    ok &= tr["summary"]["pass"] == 0 and tr["summary"]["fail"] == 0
if expect_fix == "none":
    ok &= fx is None
elif expect_fix == "opened":
    ok &= fx is not None and fx.get("number") is not None and not fx.get("unfixed")
elif expect_fix == "none-unfixed":
    ok &= fx is None or fx.get("unfixed", []) != []
sys.exit(0 if ok else 1)
PY

  popd >/dev/null
done

echo "==> e2e: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

#!/usr/bin/env bash
#
# Atomically bump the PRoctor action pin in the consumer's workflow,
# commit the change, optionally push. Designed to be a SINGLE Bash
# tool call from the /proctor:proctor-init wizard so the AI doesn't
# stall between edit → diff → commit → push.
#
# v0.3.x and v0.4.5 wizard runs split this into 3-4 separate tool
# calls (Edit → git diff → git commit → git push) and the AI
# typically Crunched / Brewed / Cooked for 1-5 minutes between each.
# User had to type "继续" 3 times per run. Wrapping the whole flow
# in one script eliminates the "what to do next" decision points.
#
# Usage:
#   wizard_bump_action.sh <new-version> [--no-push]
#
# Example:
#   wizard_bump_action.sh v0.4.6
#       (edits workflow, diffs, commits, pushes)
#   wizard_bump_action.sh v0.4.6 --no-push
#       (skips push — useful when the user explicitly opted out)
#
# Exit codes:
#   0  - bump applied (or already at target version)
#   2  - workflow file not found
#   3  - workflow doesn't reference zealllot/proctor/github-action
#   4  - push failed (commit still happened)

set -eu

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <new-version> [--no-push]" >&2
    exit 2
fi

NEW_VERSION="$1"
DO_PUSH="yes"
[ "${2:-}" = "--no-push" ] && DO_PUSH="no"

WORKFLOW=".github/workflows/proctor.yml"

if [ ! -f "$WORKFLOW" ]; then
    echo "ERR: $WORKFLOW not found — is this a PRoctor-integrated repo?" >&2
    exit 2
fi

# Extract current pin. Match zealllot/proctor/github-action@vX.Y.Z (and
# optional pre-release suffix). Use awk for portability — BSD grep on
# macOS doesn't support \d.
CURRENT_PIN=$(awk '
    match($0, /zealllot\/proctor\/github-action@v[0-9]+\.[0-9]+\.[0-9]+([-A-Za-z0-9.]*)/, m) {
        print m[0]; exit
    }' "$WORKFLOW" 2>/dev/null | sed 's|.*@||' || true)

if [ -z "$CURRENT_PIN" ]; then
    # Fall back to grep -oE for systems with gawk missing — POSIX awk
    # doesn't have the match()-with-array form.
    CURRENT_PIN=$(grep -oE 'zealllot/proctor/github-action@v[0-9]+\.[0-9]+\.[0-9]+([-A-Za-z0-9.]*)?' "$WORKFLOW" \
                  | head -1 \
                  | sed 's|.*@||' || true)
fi

if [ -z "$CURRENT_PIN" ]; then
    echo "ERR: $WORKFLOW doesn't reference zealllot/proctor/github-action — is this the right workflow file?" >&2
    exit 3
fi

if [ "$CURRENT_PIN" = "$NEW_VERSION" ]; then
    echo "Pin already at $NEW_VERSION; nothing to do."
    exit 0
fi

# In-place edit (portable BSD sed + GNU sed both)
if sed --version >/dev/null 2>&1; then
    # GNU sed
    sed -i "s|zealllot/proctor/github-action@$CURRENT_PIN|zealllot/proctor/github-action@$NEW_VERSION|" "$WORKFLOW"
else
    # BSD sed (macOS)
    sed -i '' "s|zealllot/proctor/github-action@$CURRENT_PIN|zealllot/proctor/github-action@$NEW_VERSION|" "$WORKFLOW"
fi

echo "=== diff ==="
git --no-pager diff "$WORKFLOW"
echo

# Commit. Use --quiet to keep output compact; rely on the diff above
# for user's review.
git add "$WORKFLOW"
if git diff --cached --quiet; then
    echo "(no staged changes — pin was already $NEW_VERSION or workflow was already dirty)"
else
    git commit --quiet -m "ci: bump PRoctor action $CURRENT_PIN → $NEW_VERSION"
    echo "✓ Committed pin bump $CURRENT_PIN → $NEW_VERSION"
fi

if [ "$DO_PUSH" = "no" ]; then
    echo
    echo "--no-push passed; run \`git push\` when ready."
    exit 0
fi

echo
echo "=== push ==="
if git push 2>&1 | sed 's/^/  /'; then
    echo "✓ Pushed"
    exit 0
else
    PUSH_RC=${PIPESTATUS[0]:-1}
    echo "⚠ git push exited $PUSH_RC — review the output above and push manually." >&2
    exit 4
fi

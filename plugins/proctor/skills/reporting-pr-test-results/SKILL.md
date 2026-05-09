---
name: reporting-pr-test-results
description: Use as the final stage of /proctor to render TestResults + FixPRRef into a markdown PR comment and post it. Output is the markdown body actually posted.
---

# Reporting PR Test Results

Input: `test-results.json`, `fix-pr-ref.json` (may be `null`), `change-map.json`, env (PR number, run-id, repo).

Output: a markdown comment body. The skill posts the comment via
`scripts/post_comment.py`.

## Markdown structure

```markdown
## PRoctor report — PR #<num> @ <head_sha[:7]>

**Summary:** <pass>/<total> passed · <fail> failed · <skipped> skipped
**Run id:** `<run-id>`

### Results

| ID | Category | What | Status | Notes |
|---|---|---|---|---|
| t-001 | frontend | LoginButton renders | ✅ | – |
| t-002 | api      | Display name length validated | ❌ | timeout |

### Auto-fix

[choose ONE]
- "Opened fix PR: #<fix-pr-num> covering <ids>." (when FixPRRef ≠ null and unfixed empty)
- "Opened fix PR: #<fix-pr-num> covering <ids>. **Couldn't fix** <ids>: needs human review." (partial)
- "Failures couldn't be auto-fixed. Needs human review." (FixPRRef null but auto_fix true)
- "Auto-fix disabled — see failures above." (auto_fix false)
- "All passed — nothing to fix." (no failures)

### Logs

Per-item logs: `.proctor/runs/<run-id>/<id>.log` (Action artifact in CI).
```

## Procedure

1. Render the markdown above with the actual values.
2. Compute a one-line summary `<pass>/<total> passed` for use in the gist fallback.
3. Call `post_comment.post(pr_number=..., repo=..., body=<rendered>, summary_for_gist=<one-line>)`.
4. Emit the rendered markdown to stdout (so command-level logs show what was posted).

## Constraints

- Use ✅ / ❌ / ⏭ for pass / fail / skipped.
- For failures, include `reason` in the Notes column.
- No exceptions to the table format — downstream tooling may parse it.

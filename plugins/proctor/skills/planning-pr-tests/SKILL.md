---
name: planning-pr-tests
description: Use when /proctor has a ChangeMap and needs to produce a concrete TestPlan — one test item per behavior worth verifying. Second stage of the PRoctor pipeline. Output is a single JSON object — no prose. Use when handed `change-map.json` and optionally `.pr-test.yml`.
---

# Planning PR Tests

Input: a `ChangeMap` JSON (output of analyzing-pr-changes) and optionally
the contents of the repo's `.pr-test.yml`.

Output: a single JSON object matching the `TestPlan` contract.

## Procedure

1. For each hunk in `change-map.json`, generate **one or more** test
   items. Map category → tool:

   | Category | Tool |
   |---|---|
   | `frontend` | `chrome-devtools` |
   | `api` | `bash` (curl or repo's test command) |
   | `schema` | `bash` (run migration up + down on a throwaway DB) |
   | `infra` | `bash` (build dry-run, actionlint) |
   | `mobile` | `chrome-devtools` (mobile viewport) + `bash` (lint) |
   | `cli` | `bash` (run binary, golden-file diff) |
   | `docs` | `lint-only` (no execution) |

2. **e2e-flow rule**: if `categories_present` contains BOTH `frontend`
   and `api`, append at least one extra item with `category: "e2e-flow"`
   that exercises the user-visible path involving both layers. Use
   `tool: "chrome-devtools"` and write `how:` as a short scripted
   journey.

3. Each item gets a unique `id` (`t-001`, `t-002`, ...) in declaration
   order. Use `depends_on` only when one test must run after another
   (e.g., schema migration must precede api tests against new columns).

4. Set `risk` per item based on the underlying hunk's risk and the
   blast radius of failure.

5. If `.pr-test.yml` provides `test_focus`, weight more items toward
   those categories; do not omit other categories entirely.

## Output JSON shape

```jsonc
{
  "items": [
    {
      "id": "t-001",
      "category": "frontend",
      "what": "LoginButton renders with correct label",
      "how": "Navigate to base_url; assert button[name='Sign in'] is visible",
      "tool": "chrome-devtools",
      "risk": "low",
      "depends_on": []
    }
  ]
}
```

## Constraints

- Emit exactly one JSON object. No prose.
- IDs must be unique. `depends_on` must reference IDs that exist in the same plan.
- `tool` must be one of: `chrome-devtools`, `bash`, `curl`, `lint-only`, `skip`.

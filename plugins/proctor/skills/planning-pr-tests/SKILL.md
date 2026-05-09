---
name: planning-pr-tests
description: Use when /proctor has a ChangeMap and needs to produce a concrete TestPlan — one test item per behavior worth verifying. Second stage of the PRoctor pipeline. Output is a single JSON object — no prose. Use when handed `change-map.json` and optionally `.pr-test.yml`.
---

# Planning PR Tests

Input: a `ChangeMap` JSON (output of analyzing-pr-changes) and optionally
the contents of the repo's `.pr-test.yml`.

Output: a single JSON object matching the `TestPlan` contract.

## Tool selection priority (read FIRST)

For every test item, pick the **cheapest tool that can verify the
change**, in this order. Only escalate to the next tier when the
current one cannot answer the question.

1. **`lint-only`** — pure source-level facts. Examples: an attribute
   was added (`aria-label="..."`, `type="button"`); an identifier was
   renamed; a comment was updated; a markdown table is well-formed; a
   YAML/JSON file parses. Verify via grep/awk/jq against the diff or
   the file at PR head.

2. **`bash` running the repo's existing test suite** — when the diff
   touches code that the repo's own tests cover. Look for:
   `package.json` scripts (`test`, `vitest`, `jest`), `pytest.ini` /
   `pyproject.toml`, `go test ./...`, `Cargo.toml`, `Makefile`
   targets. If a relevant target exists, plan ONE item that runs it
   scoped to the changed paths (e.g. `pytest tests/api/`,
   `go test ./api/...`, `pnpm test --run -- src/components/Login`).

   **BEFORE planning a `package.json test` run, READ the actual `test`
   script body.** Many projects keep a stub like
   `"test": "echo \\"Error: no test specified\\" && exit 1"` from
   `npm init` and never replace it. Treat any of these as "no test
   runner configured" and downgrade to tier 1 (lint-only) or 3
   (curl/chrome-devtools):
   - The script body contains `"no test specified"`, `"echo"` followed
     by `exit 1`, or is exactly `"echo ..."`-only.
   - There's no `vitest`, `jest`, `mocha`, `playwright`, `cypress`, or
     similar in `dependencies` / `devDependencies`.
   - There's no `tests/` or `__tests__/` directory anywhere with files
     matching `*.test.*` / `*.spec.*`.

   The same pattern applies for Python (`pytest` not in
   `requirements.txt` or `pyproject.toml`), Go (`*_test.go` absent
   under any package the diff touches), Rust (`#[test]` absent), etc.
   Don't propose a runner that doesn't exist.

3. **`bash` with `curl`** — API contract verification when the repo's
   `.pr-test.yml setup:` actually starts a server. The planner can
   know this by inspecting `.pr-test.yml`: if `setup:` is empty or
   missing, **do not plan** curl items against `base_url`.

4. **`chrome-devtools`** — visible UI behavior, real user
   interactions, visual regressions. Most expensive; reserve for
   things steps 1–3 cannot verify. Same pre-flight as curl: only plan
   chrome-devtools items if `.pr-test.yml setup:` brings up a server,
   otherwise plan a `lint-only` item that checks the source.

5. **`skip`** — only when the change genuinely cannot be verified
   (e.g. behavior depends on external network state we can't reach,
   or the change is purely cosmetic in a binary asset).

When a behavior can ONLY be verified at runtime but `setup:` is
missing, plan a `lint-only` item that grep-checks the source AND
mark `risk: high` so the operator sees an environment was missing.

## Procedure

1. For each hunk in `change-map.json`, decide what behavior changed,
   then walk the priority above and pick the cheapest tool. The
   category → tool mapping below is a **fallback default**, not a
   forcing function:

   | Category | Default tool when nothing cheaper fits |
   |---|---|
   | `frontend` | `chrome-devtools` (only if `setup:` brings up a UI server) |
   | `api` | `bash` (existing `*_test.go` / `pytest` / curl when server runs) |
   | `schema` | `bash` (migration up + down on a throwaway DB) |
   | `infra` | `bash` (build dry-run, actionlint) |
   | `mobile` | `chrome-devtools` (mobile viewport) + `bash` (lint) |
   | `cli` | `bash` (run binary, golden-file diff) |
   | `docs` | `lint-only` (no execution) |

2. **e2e-flow rule**: if `categories_present` contains BOTH `frontend`
   and `api`, append at least one extra item with `category: "e2e-flow"`
   that exercises the user-visible path involving both layers. Use
   `tool: "chrome-devtools"` and write `how:` as a short scripted
   journey. **Skip this rule** if `.pr-test.yml setup:` doesn't bring
   up both layers.

3. Each item gets a unique `id` (`t-001`, `t-002`, ...) in declaration
   order. Use `depends_on` only when one test must run after another
   (e.g., schema migration must precede api tests against new columns).

4. Set `risk` per item based on the underlying hunk's risk and the
   blast radius of failure.

5. If `.pr-test.yml` provides `test_focus`, weight more items toward
   those categories; do not omit other categories entirely.

6. **Use `pr_context` from the ChangeMap to drive what each item actually verifies.** The PR description often contains the real acceptance criteria — they're rarely visible from the diff alone. For the items you generate:
   - Read `pr_context.title`, `pr_context.body`, and `pr_context.requirement_hints`. Treat the body as the source of truth for "what this change is supposed to do".
   - When the body says something concrete (e.g. "max 100 chars", "rate limit 60/min", "must show toast on save"), generate an item that verifies that exact thing — phrase the item's `what:` field in the body's wording, and write `how:` against the actual constraint, not against what the diff merely allows.
   - When the body links to Slack / Jira / Linear / Notion / Confluence (`pr_context.links`), do NOT try to fetch them — just acknowledge the requirement is documented there. In `how:`, you can write `Per <ticket-id>: ...` so the report makes the link traceable. If the body doesn't quote the requirement and only links to it, fall back to whatever you can infer from the diff and mark `risk: medium` to flag that the off-PR doc was the load-bearing source of truth.
   - When `requirement_hints` and the diff disagree, plan items for BOTH: one that verifies the body's stated behavior, one that verifies the diff's actual behavior. The mismatch is itself useful signal in the report.
   - If `pr_context` is empty or absent, fall back to inferring tests from the diff alone — same as before.

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

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

## Coverage balance (read this BEFORE writing the items array)

**The most important test for any new feature is "the feature works." Negative / validator-rejects-bad-input tests are useful but secondary — if they pass while the happy path fails, you've verified the cage is locked while the building burns down.**

For every new behavior the PR introduces (every distinct user-facing path the diff enables), the plan MUST include at least one happy-path item BEFORE adding negative items. Mechanically:

1. Read `pr_context.body` and identify the user-stories / checklist items. Most PR bodies have phrases like "feature X works", "user can do Y and Z saves correctly", "save & publish works" — these are happy paths.
2. For each happy-path phrase, draft an item that constructs the FULL successful flow: fill the form with valid data → submit → assert success (200/302, success toast, persisted record visible in list / detail page).
3. THEN add negative items for validators / edge cases / error states. Aim for ≤ 1 negative item per validator branch, not 1 per typo / one per invalid value.
4. If you find yourself writing 4 chrome-devtools items and all 4 are "submit X with bad input, expect error" — STOP. Replace one or two with the corresponding "submit X with good input, expect success" variant.

Concrete example for a PR titled "add Digital Reward type=Image / type=Game":

```
✗ All-negative plan (what the AI naturally drafts):
  t-006  Form renders with new fields
  t-007  Validator: missing type → error
  t-008  Validator: Image + empty asset → error
  t-009  Validator: Game + empty URL → error
  t-010  Validator: Game + invalid URL → error

✓ Balanced plan (what to actually write):
  t-006  Form renders with new fields
  t-007  HAPPY: Save Image reward with valid asset → 200, appears in list, published
  t-008  HAPPY: Save Game reward with valid URL → 200, appears in list, published
  t-009  NEGATIVE: missing type → error (single validator-coverage check)
  t-010  NEGATIVE: Game + invalid URL → error (chosen because URL parse is the most likely
         place to silently relax)
```

Two happy + two negatives gives the reviewer signal that the feature actually works AND that the most important guard rails fire. Five negatives gives signal that bad input gets rejected but says nothing about whether the feature itself ships.

When the PR body explicitly lists more negative cases than happy ones (rare, but happens for security-hardening PRs), respect that — but always include at least one happy-path item per new code path.

## Role-aware planning (when `.pr-test.yml` has `auth.accounts`)

If the consumer's `.pr-test.yml` declares an `auth` block with an `accounts` array, this admin has role-based permissions and you can target specific roles per item.

Each item may carry an optional `as_account: <name>` field that references one of the `auth.accounts[].name` values. When omitted, the executor uses `accounts[0]` (by convention the highest-privilege account).

Use `auth.accounts[].role_label` for context — it's a one-line human description of what each role can do (e.g. `"Developer (full admin)"` vs `"Editor (content only)"` vs `"Read-only viewer"`). Read those labels to decide which account each item should run as.

When to plan multi-role items (same behavior, multiple accounts):

- **The diff changes a permission check, role definition, or visibility rule** → plan the same check as several items, one per relevant account, with expected outcomes that DIFFER per role. Example: a diff adds `if !user.IsDeveloper { hide(deleteButton) }` — plan three items:
  - `t-002` as `developer`: delete button IS visible
  - `t-003` as `editor`:    delete button is NOT visible
  - `t-004` as `viewer`:    delete button is NOT visible
- **The diff adds a feature that's only meaningful for certain roles** → plan as the role(s) that actually see the feature, plus one negative item from a lower role.
- **The diff is role-agnostic** (schema migration, layout polish, doc update) → leave `as_account` unset; everything runs as `accounts[0]`. Do NOT explode trivially-role-invariant items across all accounts; that just multiplies cost.

Phrasing tip: when an item is role-specific, put the role in the `what:` field — e.g. `what: "As editor, delete button is hidden on /admin/users/:id"`. Makes the report easy to scan.

## Output JSON shape

Every item MUST carry a `rationale` field — a one-paragraph explanation of WHY this test was generated for THIS diff. The report renders it as "Why this test" so the developer can audit the planner's reasoning. Without it, the dev sees test items appearing out of nowhere and can't tell whether the AI understood the change. Tie the rationale to one or more of:

- a specific hunk in the ChangeMap (cite the file + brief description of what changed there)
- a claim in `pr_context.body` / `pr_context.requirement_hints` (quote the relevant phrase)
- a category-level risk (e.g. "schema change → migrations need verifying")

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
      "depends_on": [],
      "rationale": "The diff in src/auth/LoginButton.tsx adds an aria-label and changes the visible text from 'Login' to 'Sign in'. This item verifies the new visible text is what users see at /login."
      // "as_account" omitted → executor uses accounts[0]
    },
    {
      "id": "t-002",
      "category": "frontend",
      "what": "As editor, Users menu item is hidden",
      "how": "Navigate to /admin; assert no nav link with text 'Users'",
      "tool": "chrome-devtools",
      "risk": "medium",
      "depends_on": [],
      "as_account": "editor",
      "rationale": "The diff in admin/permissions.go added `if !user.IsDeveloper { hide(\"users\") }`. The corresponding negative check (editor / viewer) verifies the new permission gate actually hides the menu item for non-developers."
    }
  ]
}
```

Rationale writing rules:
- One paragraph, 1–3 sentences. Not a wall of text.
- Cite at least one concrete signal from the diff or PR body. "Generated because the PR touches frontend code" is too vague.
- For multi-role items, the rationale should explain why this SPECIFIC role is the right one to test under.

## Constraints

- Emit exactly one JSON object. No prose.
- IDs must be unique. `depends_on` must reference IDs that exist in the same plan.
- `tool` must be one of: `chrome-devtools`, `bash`, `curl`, `lint-only`, `skip`.
- When set, `as_account` must equal one of `auth.accounts[].name` values from `.pr-test.yml`. The validator rejects unknown names.

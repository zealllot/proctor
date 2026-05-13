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

## Journey-first planning (write BEFORE the items array)

Don't plan hunk-by-hunk. Read the PR body + ChangeMap first, then derive **1–3 user journeys** — concrete sequences a real person walks through to use the feature. ONLY THEN write the items, grouped by journey.

A user journey is: a goal + an ordered set of steps + a final-state assertion. Example for the Digital Reward type=Image/Game PR:

```
Journey 1: "Create-Image-Reward"
  Goal: Admin creates a published Image-type digital reward.
  Steps:
    1. (precondition) Logged in as developer, no existing reward with this name.
    2. Open /admin/rewards/new
    3. Select Type = Image, fill asset + name
    4. Click Save
    5. Click Publish
    6. Verify: reward appears in list with status=Published.
  After-state: navigate away, navigate back, reload — record still there.

Journey 2: "Create-Game-Reward"
  Goal: Same as Journey 1 but for Game type, requiring a valid URL.
  Steps: <similar>

Journey 3: "Reject-Bad-Game-URL"
  Goal: A reward submitted with an invalid Game URL is rejected with a clear error.
  Steps:
    1. Logged in as developer.
    2. Open form, select Type=Game.
    3. Enter GameUrl = 'not-a-url'.
    4. Click Save.
    5. Verify: 422 with "Game URL is not a valid URL"; form not submitted.
```

Tag every plan item with the `journey` field naming its journey. Items within a journey list each other in `depends_on` if they share state — e.g. the "verify list still has it" item depends on the "save it" item. Items in different journeys are independent.

Why journeys: reviewers think about features as "did the create-publish flow work end-to-end", not "did 7 isolated assertions pass". Grouping items by journey gives the report a structure that maps to product behavior. Also forces the planner to think "what's the full happy path" before getting absorbed in negative-case minutiae.

How many journeys: **1–3**. More than 3 means you're over-segmenting; the diff probably has fewer cohesive user-facing flows than that. Single-flow PRs (a typo fix, a docs change, an internal refactor) can have ZERO journeys — just a flat item list — that's fine.

## Impact-aware regression coverage (`impact_radius`)

For each hunk the analyzer flags with a non-empty `impact_radius` list,
that list names files that **import / reference the changed symbol(s)**
and may regress. Treat these as additional surface to cover, NOT as
items to test directly:

- **High-impact hunk** (`impact_radius` has 5+ files) → plan ONE
  regression item that exercises the most likely caller path. Pick the
  caller that's closest to user-visible behavior (a `handler.go` /
  `router.go` / `*_screen.tsx` beats an internal `helpers.go`). Cite
  the caller file in the item's `rationale`.
- **Medium-impact hunk** (1–4 files) → optional extra item. Add it
  only if the diff modified the function's signature, return shape, or
  side-effect contract — not for pure-additive changes.
- **Empty `impact_radius: []`** (analyzer looked, found nothing) →
  treat as a leaf change; no regression items needed.
- **`impact_radius` field missing** (analyzer didn't run, e.g. docs
  hunk) → treat as legacy; plan as before.

Regression items SHOULD be tagged with `category` matching the caller
(e.g. caller is `admin/rewards/router.go` → category `api`). Do not
explode N items per caller — one item that walks the most user-visible
caller is the goal. The blast-radius signal is "how many fan-out
files exist", not "test every fan-out file".

Phrase the item's `what:` so a reader sees the regression intent:

> `what: "REGRESSION: dashboard widget that reads CreateReward still renders after Type=Image branch added"`

`impact_radius` is advisory, not authoritative. False positives are
expected (the grep is identifier-name-based, not type-aware). When you
see a caller you suspect is a false-positive based on the file name,
skip it.

## Item-to-item data dependency (`data_from`)

When item B is meaningful ONLY IF item A succeeded (A creates a record, B edits that record), declare it explicitly:

```jsonc
{
  "id": "t-005",
  "what": "HAPPY: Save Image reward 'fixture-image-1'",
  "journey": "Create-Image-Reward",
  "depends_on": [],
  // ...
},
{
  "id": "t-006",
  "what": "Edit Image reward 'fixture-image-1': replace asset",
  "journey": "Create-Image-Reward",
  "depends_on": ["t-005"],     // execution-order dep
  "data_from": ["t-005"],      // STATE dep — if t-005 fails, skip t-006
  // ...
}
```

`depends_on` orders execution. `data_from` says "the world-state I need can only be set up by these items succeeding". If a `data_from` source fails or is skipped, the executor marks this item `skipped` (not `fail`) with reason `data-dep-failed: t-005`. Without `data_from`, item B would have run and errored ambiguously — was it B's logic that broke, or was the test invalidated upstream?

Use `data_from` when:
- B reads a record A just created
- B asserts an effect that only happens after A's side-effect
- B targets a URL whose path includes A's generated ID

DON'T use `data_from` for items that share fixture data but don't share live-test state — those are independent runs, no skip-on-upstream-fail.

The schema enforces: every `data_from` entry must also appear in `depends_on` (data dependency implies ordering).

### Passing artifacts from producer to consumer (`produces` + `{{...}}` templates)

`data_from` alone only says "if A failed, skip B". The piece that closes the loop is the actual value B needs from A — typically a generated record ID or URL. The plan declares this explicitly:

1. **On the producer**, declare every output the consumer will read via `produces: ["<key>", ...]`. Keys must be identifier-ish (`[A-Za-z_][A-Za-z0-9_]*`) since they're used inline in shell-ish substitution.
2. **In the consumer's `how:` or `preconditions`**, reference the value with `{{<producer_id>.<key>}}`. The executor substitutes it before dispatching the consumer subagent.

```jsonc
{
  "id": "t-005",
  "what": "HAPPY: Save Image reward 'fixture-image-1'",
  "journey": "Create-Image-Reward",
  "how": "Navigate to /admin/rewards/new; Type=Image; Name='fixture-image-1'; pick asset; Save; capture the numeric record ID from the resulting /admin/rewards/<id> URL.",
  "depends_on": [],
  "produces": ["created_id", "detail_url"]
},
{
  "id": "t-006",
  "what": "Edit Image reward 'fixture-image-1': replace asset",
  "journey": "Create-Image-Reward",
  "preconditions": "Logged in as developer. Record exists at {{t-005.detail_url}}.",
  "how": "Navigate to {{t-005.detail_url}}; click Edit; replace asset; Save; assert new asset displays.",
  "depends_on": ["t-005"],
  "data_from": ["t-005"]
}
```

**Key rules:**
- Only declare `produces` for keys downstream items actually consume. Don't speculate.
- Every `{{<id>.<key>}}` template MUST point to an item in this item's `data_from` and that item MUST list `<key>` in its `produces`. Schema rejects orphan references at validation time so the executor never hits a runtime "what does this template mean" surprise.
- When the upstream subagent returns `outputs` missing a declared key, the upstream is FAILED (not the downstream) — the contract was promised, not kept.
- Use canonical key names: `created_id`, `record_id`, `detail_url`, `slug`. Don't invent per-item names; the planner should treat these like a small vocabulary so reviewers see the same words across journeys.

When NOT to use templates:
- The "consume A's state" is implicit (e.g. B just visits a list page A added to — no specific URL or ID needed). Use `data_from` without `produces`.
- The artifact is something already known statically (a fixture seed name). Hardcode it in both items; no template needed.

## Write-operation persistence — required assertions

For any test item that performs a write (POST/PUT/DELETE, form submit, file upload, state-changing button click), the immediate response (toast / 200 / element visible) is necessary but NOT sufficient. The reviewer needs to know the change actually persisted.

For every write-action item, append these checks to `how:`:

1. **Confirm the immediate response** (existing — toast, 200, page transition).
2. **Navigate away** to the list page or another route, then **navigate back** to the just-edited resource.
3. **Assert the persisted state** — the field value, record presence in list, status badge — is what was just submitted.
4. **(If the app supports it) page reload** between steps 2 and 3 — flushes client-side state and proves the value came from the server, not from memory.

Concrete examples:

```
✗ Insufficient:
  "Fill form → click Save → assert success toast visible"

✓ With persistence:
  "Fill form → click Save → assert success toast visible →
   navigate to /admin/rewards (list page) → assert new record row
   visible with the submitted name → click into the record → reload
   the detail page → assert all fields match what was submitted"
```

If the immediate response IS the persistence (e.g. a generated UUID returned by the API and shown in the URL), that's fine — call it out in the `how:` so a reader knows the test understood the difference. Don't silently rely on "the toast says saved".

## Preconditions: don't bury the starting state in `how:`

When a test requires a specific starting state (logged-in role, seeded data, no existing record with conflicting name, feature flag enabled, etc.), put it in a separate `preconditions` field, NOT mixed into `how:`. Keeps the action steps focused; keeps it obvious to the executor what to verify-or-establish before the test action begins.

```jsonc
{
  "id": "t-005",
  "what": "Editing an existing Image reward updates the asset",
  "preconditions": "Logged in as developer. An Image-type reward named 'fixture-edit-test' already exists (created by t-004 or via seed).",
  "how": "Navigate to /admin/rewards; find 'fixture-edit-test' row; click Edit; replace the asset; click Save; assert new asset displays.",
  "tool": "chrome-devtools",
  "category": "api",
  "risk": "high",
  "depends_on": ["t-004"],
  "rationale": "..."
}
```

Don't write `preconditions` for items where the only precondition is "PRoctor is logged in" (that's covered by the auth flow). Use it when there's a non-trivial state that must exist for the test to be meaningful.

## Error coverage: vary the `error_type`, don't repeat one kind

When you plan more than one negative item, distribute across `error_type` categories instead of stacking the same kind. Valid values:

| `error_type` | When it fires |
|---|---|
| `validation` | form / input validator rejects bad data (empty, wrong format, out of range) |
| `permission` | role-based access check denies action (editor tries to delete) |
| `network` | upstream API fails, timeout, retry exhausted |
| `state-conflict` | concurrent edit, duplicate submission, stale-data write |
| `not-found` | 404 / record was deleted by another user |
| `auth` | session expired, not logged in, csrf token rejected |

**Coverage rule**: among all your negative items, at most ~2 should share an `error_type`. If you find 4 negatives all `validation`, replace 2 with the `permission` / `state-conflict` / `not-found` variant that's actually present in the diff. Each error_type covered ≈ one new failure-mode class verified.

For happy-path items, leave `error_type` unset.

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
      "preconditions": "Logged in as editor (auth.accounts[name=editor]).",
      "how": "Navigate to /admin; assert no nav link with text 'Users' in the sidebar nav region.",
      "tool": "chrome-devtools",
      "risk": "medium",
      "depends_on": [],
      "as_account": "editor",
      "error_type": "permission",
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

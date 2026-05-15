---
name: planning-pr-tests
description: Use when /proctor has a ChangeMap and needs to produce a concrete TestPlan — one test item per behavior worth verifying. Second stage of the PRoctor pipeline. Output is a single JSON object — no prose. Use when handed `change-map.json` and optionally `.proctor/config.yml`.
---

# Planning PR Tests

Input: a `ChangeMap` JSON (output of analyzing-pr-changes) and optionally
the contents of the repo's `.proctor/config.yml`.

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
   `.proctor/config.yml setup:` actually starts a server. The planner can
   know this by inspecting `.proctor/config.yml`: if `setup:` is empty or
   missing, **do not plan** curl items against `base_url`.

4. **`chrome-devtools`** — visible UI behavior, real user
   interactions, visual regressions. Most expensive; reserve for
   things steps 1–3 cannot verify. Same pre-flight as curl: only plan
   chrome-devtools items if `.proctor/config.yml setup:` brings up a server,
   otherwise plan a `lint-only` item that checks the source.

5. **`skip`** — only when the change genuinely cannot be verified
   (e.g. behavior depends on external network state we can't reach,
   or the change is purely cosmetic in a binary asset).

When a behavior can ONLY be verified at runtime but `setup:` is
missing, plan a `lint-only` item that grep-checks the source AND
mark `risk: high` so the operator sees an environment was missing.

## Read the repo's docs FIRST (v0.7.5+, mandatory)

**Before writing any plan items, scan the consumer repo's documentation** — `README.md`, `CLAUDE.md` (and any other `CLAUDE.md` files under subdirs), `AGENTS.md`, `GEMINI.md`, the `docs/` tree, and any in-tree `*.md` files mentioned by the PR body. These typically encode:

- Project conventions (test naming, fixture layout, how to run things locally)
- Domain rules the diff is implementing against (validators, business invariants, role permissions)
- Acceptance criteria for the type of change at hand (e.g. "all admin form changes need a round-trip test")
- Architectural constraints the diff must respect (e.g. "validators are registered in `<file>:<func>`; new ones go there")

Read targeted, not exhaustive:

| What | Why |
|---|---|
| `README.md` (root) | Stack / how-to-run / convention pointers |
| `CLAUDE.md` (root + any subdir CLAUDE.md the diff touches) | AI-collaboration rules + domain notes the maintainers want you to obey |
| `docs/<topic>.md` when filename clearly relates to the diff (e.g. diff touches `models/foo/` → check `docs/foo.md` or `docs/models.md`) | Authoritative behavior spec |
| Any path the PR body explicitly cites | The PR author already pointed at the doc; not reading it is a planning gap |

Skip when none of these exist — small repos don't carry docs.

**Use what you find in `rationale:` and `how:`.** Each item's `rationale:` field should cite the doc/spec that defines the behavior being verified (e.g. `Per CLAUDE.md "All admin form changes need round-trip", t-006 covers reload after save.`). `how:` can reference doc-stated values directly (e.g. `validator at api.go:88-104 rejects empty Title per <docs/validators.md>`). Reviewers trust the plan more when items trace back to documented intent, not just diff inference.

**Cite linked sources explicitly in `rationale:` when they exist.** When `pr_context.linked_content[]` contains entries with `fetched: true`, the planner has actual content (not just URLs) — quote them where they drove a planning decision. Examples:

- `Per [Slack: ts=p1777...]: "comma-separated tokens agreed"; t-005 verifies a comma in IncludeTags is accepted as the token separator.`
- `Per [Jira MDX-12659 description]: "trim instead of reject for backward compat"; t-007 covers the trim path.`
- `Per [GitHub issue #523 comments]: maintainer pushed back on the 422 response and asked for 200+toast; t-003 asserts toast not 422.`

`pr_context.comments[]` entries are also citation-worthy when a maintainer's comment changes scope. Use `Per [PR comment by @alice 2026-05-12]: "..."` so the report makes the comment traceable.

### Doc-link traversal — follow internal markdown links one level deep (v0.7.6+)

After reading the top-level docs (`README.md` / `CLAUDE.md` / `AGENTS.md` / `docs/testing-notes.md` / `docs/patterns.md` / etc.), follow internal markdown links (`[text](path.md)` form) ONE level deep when the linked doc's filename suggests it's testing-related. Specifically follow when the linked filename contains any of: `test`, `testing`, `publishing`, `environments`, `deploy`, `auth`, `validators`, `patterns`, `conventions`, `e2e`, `journeys`, `runbook`. Skip filenames that look like generic indexes (`README.md`, `index.md`) — they'd cascade endlessly.

Constraints to keep this bounded:
- Cap at 5 followed links per analyze session.
- Skip when the same doc is already on the read-list (de-dupe by path).
- Only follow relative paths in the same repo. Skip absolute URLs and links to other repos.
- If a followed doc is empty / 404 / a binary asset, skip and don't retry.

The followed docs feed the same `rationale:` discipline above — when a planning decision came from a 2nd-level doc, cite it (e.g. `Per docs/testing-notes.md → docs/journeys/save-flow.md: "every save flow must assert toast + reload"`).

**When repo docs conflict with the diff**, plan items for BOTH — same rule as `pr_context.requirement_hints` vs diff. The mismatch is signal.

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
   journey. **Skip this rule** if `.proctor/config.yml setup:` doesn't bring
   up both layers.

3. Each item gets a unique `id` (`t-001`, `t-002`, ...) in declaration
   order. Use `depends_on` only when one test must run after another
   (e.g., schema migration must precede api tests against new columns).

4. Set `risk` per item based on the underlying hunk's risk and the
   blast radius of failure.

5. If `.proctor/config.yml` provides `test_focus`, weight more items toward
   those categories; do not omit other categories entirely.

6. **Use `pr_context` from the ChangeMap to drive what each item actually verifies.** The PR description often contains the real acceptance criteria — they're rarely visible from the diff alone. For the items you generate:
   - Read `pr_context.title`, `pr_context.body`, and `pr_context.requirement_hints`. Treat the body as the source of truth for "what this change is supposed to do".
   - When the body says something concrete (e.g. "max 100 chars", "rate limit 60/min", "must show toast on save"), generate an item that verifies that exact thing — phrase the item's `what:` field in the body's wording, and write `how:` against the actual constraint, not against what the diff merely allows.
   - When the body links to Slack / Jira / Linear / Notion / Confluence (`pr_context.links`), do NOT try to fetch them — just acknowledge the requirement is documented there. In `how:`, you can write `Per <ticket-id>: ...` so the report makes the link traceable. If the body doesn't quote the requirement and only links to it, fall back to whatever you can infer from the diff and mark `risk: medium` to flag that the off-PR doc was the load-bearing source of truth.
   - When `requirement_hints` and the diff disagree, plan items for BOTH: one that verifies the body's stated behavior, one that verifies the diff's actual behavior. The mismatch is itself useful signal in the report.
   - If `pr_context` is empty or absent, fall back to inferring tests from the diff alone — same as before.

## Trust the project's dev_launcher (v0.7.11+)

v0.7.7–v0.7.10 had this SKILL walk `.proctor/local.yml setup:` looking for `go run ./cmd/<name>` lines to know which supplementary binaries the project starts, then mapped diff hunks against `cmd/<name>/main.go` imports to decide which binaries the diff might affect at runtime. **All of that is gone in v0.7.11.** The right model: the project's launcher (`./dev.sh all` / `make dev` / `pnpm dev` / etc., declared in `.proctor/config.yml.dev_launcher.start`) brings up whatever processes the project owner says belong in a dev environment. The planner doesn't need to know what those processes are — it just needs to know whether the environment is up.

**What the planner reads.** When `.proctor/config.yml.dev_launcher` is set:

- `dev_launcher.start` — the project's launch command. Informational only; the planner doesn't substitute or analyze it.
- `dev_launcher.wait_for` (optional) — a bash command that exits 0 when the environment is fully ready. Useful as a `verify_precondition_via` value on plan items that depend on the env being up before the assertion can run.

When the wait_for command is set, plan items that hit the live env can reference it as a precondition:

```jsonc
{
  "id": "t-005",
  "what": "HAPPY: ...",
  "verify_precondition_via": "curl -fsS http://localhost:9801/healthz >/dev/null 2>&1"
  // ^ pull this from dev_launcher.wait_for verbatim
}
```

This makes "dev env didn't come up" distinguishable from "the change under test is broken" in the report (the precondition-not-met skip path was added in v0.3.29 — see schema's `verify_precondition_via` field).

**What the planner does NOT do anymore.** No `cmd/*/main.go` discovery, no import-graph reachability analysis, no `supplementary_binaries_running` / `supplementary_binary_touched` lists, no `setup_context` JSON. The deleted `plan_smells.py` rule (`missing-runtime-verify-when-supplementary-binary-present`) is gone — `pr-body-coverage` already catches "PR body mentions output X that no plan item verifies" generically. If a PR claims published output from a long-running process, plan a `bash` item that polls the output URL the same way you'd plan any other runtime-verify item; you don't need to know whether the underlying process is a daemon, worker, or scheduler — the project's launcher already started it.

**Legacy `setup:` array consumers.** Same advice — the planner reads `.proctor/local.yml setup:` only as a hint about base_url / wait-loops. The actual content of the setup array (whether it starts one binary or eight) is opaque to the planner.

**Backward compat for the deleted `no-supplementary-binary-in-setup` skip reason.** Plans written under v0.7.7–v0.7.10 may still contain `skip` items with `reason: "no-supplementary-binary-in-setup"`. The reporter still groups those into "Runtime verification gaps" — see `reporting-pr-test-results/SKILL.md`. New plans should not use this reason; if the PR genuinely can't be runtime-verified, use a generic `reason: "no-runtime-verify-possible"` and explain in `how:`.

## Coverage balance (read this BEFORE writing the items array)

**The most important test for any new feature is "the feature works." Negative / validator-rejects-bad-input tests are useful but secondary — if they pass while the happy path fails, you've verified the cage is locked while the building burns down.**

For every new behavior the PR introduces (every distinct user-facing path the diff enables), the plan MUST include at least one happy-path item BEFORE adding negative items. Mechanically:

1. Read `pr_context.body` and identify the user-stories / checklist items. Most PR bodies have phrases like "feature X works", "user can do Y and Z saves correctly", "save & publish works" — these are happy paths.
2. For each happy-path phrase, draft an item that constructs the FULL successful flow: fill the form with valid data → submit → assert success (200/302, success toast, persisted record visible in list / detail page).
3. THEN add negative items for validators / edge cases / error states. Aim for ≤ 1 negative item per validator branch, not 1 per typo / one per invalid value.
4. If you find yourself writing 4 chrome-devtools items and all 4 are "submit X with bad input, expect error" — STOP. Replace one or two with the corresponding "submit X with good input, expect success" variant.

**Vocabulary the lint recognizes** (v0.3.36+): the `plan_smells.py` "all-negative plan" check looks for these write-action verbs in each item's `what:` — `save`, `create`, `update`, `submit`, `edit`, `publish`, `upload`, `persist`, `insert`, `store` (present or past tense). Phrase happy-path items using one of these so the lint correctly counts them. Synonyms like "preserves", "stores away", "records into" won't match and the lint will (incorrectly) flag the plan as all-negative.

Worked example (replace `<Resource>` / `<RequiredField>` / `<TypedField>` with your PR's specifics — this shape applies to ANY new-form / new-field PR, regardless of domain):

```
✗ All-negative plan (what the AI naturally drafts when validators are the most visible code change):
  t-001  Form renders with new fields
  t-002  Validator: <RequiredField> missing → error
  t-003  Validator: <TypedField> wrong format → error
  t-004  Validator: <TypedField> empty when required → error
  t-005  Validator: <TypedField> out-of-range → error

✓ Balanced plan:
  t-001  Form renders with new fields
  t-002  HAPPY: Save <Resource> with all valid inputs → 200, record appears in list
  t-003  HAPPY: Save <Resource> with the alternative typed variant → 200, record appears in list
         (only if the diff introduces multiple typed paths — skip otherwise)
  t-004  NEGATIVE: <RequiredField> missing → field-level error (covers the most likely validator gap)
  t-005  NEGATIVE: <TypedField> = <silently-relaxable invalid value> → error
         (chosen because that's the validator branch most likely to regress under refactor)
```

Two happy + two negatives gives the reviewer signal that the feature actually works AND that the most important guard rails fire. Five negatives gives signal that bad input gets rejected but says nothing about whether the feature itself ships.

When the PR body explicitly lists more negative cases than happy ones (rare, but happens for security-hardening PRs), respect that — but always include at least one happy-path item per new code path.

## Journey-first planning (write BEFORE the items array)

Don't plan hunk-by-hunk. Read the PR body + ChangeMap first, then derive **1–3 user journeys** — concrete sequences a real person walks through to use the feature. ONLY THEN write the items.

**v0.3.28+ structure** — declare journeys as a top-level array, reference from items by id:

Shape (replace placeholders with whatever your PR actually touches):

```jsonc
{
  "journeys": [
    {
      "id": "j-create-<resource>",
      "goal": "<Role> creates a <Resource> via the form and the record persists.",
      "terminal_state": "<Resource> appears in /<list_route> with <status_field>=<terminal_value>; survives a hard reload of the detail page."
    },
    {
      "id": "j-reject-<invalid-case>",
      "goal": "A <Resource> submitted with <invalid_input> is rejected with a clear field-level error.",
      "terminal_state": "Form shows field-level error; no record persisted."
    }
  ],
  "items": [
    { "id": "t-001", "journey_id": "j-create-<resource>", /* ... */ }
  ]
}
```

`id` should be human-readable kebab-case starting with `j-`. `goal` is one sentence describing what the user accomplishes; `terminal_state` is the assertable end-state the reporter cites in the journey header.

Two reasons for the structured form (over v0.3.23's loose `journey: "<name>"` string):
1. **No accidental group splits** — two items whose loose journey strings differ in case / hyphenation / pluralization would render as two separate report sections under string-match grouping. Reference-by-id eliminates that.
2. **The goal + terminal_state become part of the report header** — reviewers see WHAT the journey is supposed to verify, not just a name.

Legacy `journey: "<name>"` string still validates (backward compat with pre-v0.3.28 plans); schema rejects setting BOTH `journey` and `journey_id` on the same item.

A user journey is: a goal + an ordered set of steps + a final-state assertion. Concrete example for a hypothetical "draft + publish blog post" PR (deliberately a DIFFERENT domain from anything you might be testing — the shape is what carries over, not the entity names):

```
Journey 1: "create-and-publish-post"
  Goal: Editor drafts a new post and publishes it.
  Steps:
    1. (precondition) Logged in as editor; no post with this slug exists.
    2. Open /admin/posts/new
    3. Fill title, body
    4. Click Save → assert draft saved
    5. Click Publish
    6. Verify: post appears in /admin/posts with status=Published.
  After-state: navigate away, navigate back to /admin/posts/<slug>, hard-reload — title + body still render correctly.

Journey 2: "reject-empty-body"
  Goal: Posts with empty body are rejected with a field-level error.
  Steps:
    1. Logged in as editor.
    2. Open form; fill title only.
    3. Click Save.
    4. Verify: 422 with "Body is required"; no record persisted.
```

Substitute the resource and the actions to whatever your diff actually touches. If your PR is about a checkout flow, the shape is the same: a checkout journey with (precondition / actions / verify) + a reject-bad-input journey.

Tag every plan item with `journey_id` (preferred) or `journey` (legacy). Items within a journey list each other in `depends_on` if they share state — e.g. the "verify list still has it" item depends on the "save it" item. Items in different journeys are independent.

Why journeys: reviewers think about features as "did the create-publish flow work end-to-end", not "did 7 isolated assertions pass". Grouping items by journey gives the report a structure that maps to product behavior. Also forces the planner to think "what's the full happy path" before getting absorbed in negative-case minutiae.

How many journeys: **1–3**. More than 3 means you're over-segmenting; the diff probably has fewer cohesive user-facing flows than that. Single-flow PRs (a typo fix, a docs change, an internal refactor) can have ZERO journeys — just a flat item list — that's fine.

## Impact-aware regression coverage (`impact_radius`)

For each hunk the analyzer flags with a non-empty `impact_radius` list,
that list names files that **import / reference the changed symbol(s)**
and may regress. Treat these as additional surface to cover, NOT as
items to test directly:

- **Truncated radius** (`impact_radius_truncated: true`, v0.3.28+) →
  the 10 visible files are NOT the full blast radius (monorepo with
  100s of callers; the helper capped at 10). **Force `risk: "high"`**
  on the regression item AND on the hunk-level items targeting this
  symbol, regardless of what the original hunk risk was. Plan
  **TWO** regression items: one for the highest-fan-out caller, one
  for the next caller in a different subtree (so a single bad
  release-time test choice doesn't miss whole packages). Cite the
  truncation in `rationale`: "impact_radius truncated at top 10 of
  many — this hunk's actual fan-out is larger than visible; planned
  two regression items as a sample-of-the-iceberg."
- **High-impact hunk** (`impact_radius` has 5+ files, NOT truncated)
  → plan ONE regression item that exercises the most likely caller
  path. Pick the caller closest to user-visible behavior (a
  `handler.go` / `router.go` / `*_screen.tsx` beats an internal
  `helpers.go`). Cite the caller file in the item's `rationale`.
- **Medium-impact hunk** (1–4 files) → optional extra item. Add it
  only if the diff modified the function's signature, return shape, or
  side-effect contract — not for pure-additive changes.
- **Empty `impact_radius: []`** (analyzer looked, found nothing) →
  treat as a leaf change; no regression items needed.
- **`impact_radius` field missing** (analyzer didn't run, e.g. docs
  hunk) → treat as legacy; plan as before.

Regression items SHOULD be tagged with `category` matching the caller
(e.g. caller is `<package>/router.go` → category `api`). Do not
explode N items per caller — one item that walks the most user-visible
caller is the goal. The blast-radius signal is "how many fan-out
files exist", not "test every fan-out file".

Phrase the item's `what:` so a reader sees the regression intent:

> `what: "REGRESSION: <caller-screen-or-handler> still renders / responds correctly after <changed-function>'s new branch is added"`

`impact_radius` is advisory, not authoritative. False positives are
expected (the grep is identifier-name-based, not type-aware). When you
see a caller you suspect is a false-positive based on the file name,
skip it.

## Item-to-item data dependency (`data_from`)

When item B is meaningful ONLY IF item A succeeded (A creates a record, B edits that record), declare it explicitly:

```jsonc
{
  "id": "t-005",
  "what": "HAPPY: Create <Record> 'fixture-1' via the form",
  "journey_id": "j-create-and-edit",
  "depends_on": [],
  // ...
},
{
  "id": "t-006",
  "what": "Edit <Record> 'fixture-1': change <field>",
  "journey_id": "j-create-and-edit",
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
  "what": "HAPPY: Create <Record> 'fixture-1' via the form",
  "journey_id": "j-create-and-edit",
  "how": "Open /<route>/new; fill required fields with 'fixture-1' as the name; Save; capture the new record ID from the resulting /<route>/<id> URL.",
  "depends_on": [],
  "produces": ["created_id", "detail_url"]
},
{
  "id": "t-006",
  "what": "Edit <Record> 'fixture-1': change <field>",
  "journey_id": "j-create-and-edit",
  "preconditions": "Logged in as <role>. Record exists at {{t-005.detail_url}}.",
  "how": "Navigate to {{t-005.detail_url}}; click Edit; change <field>; Save; assert new value displays.",
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

## One assertion class per item — MANDATORY (v0.3.30+)

Each plan item MUST verify EITHER success OR a specific failure mode — never both. Items whose `what:` describes both are bugs in the plan, not features:

- The report shows one status per item; a combined item that did the negative half correctly but failed the happy half becomes "fail" with ambiguous evidence. The reviewer can't tell which half broke without reading the full log.
- The setup, action, and assertion differ between happy and negative paths. Forcing them into one item makes the assertion vague enough to pass for the wrong reason.
- The coverage-balance rule REQUIRES both happy and negative items; it does NOT say to fold them into one item.

Anti-pattern (any phrasing like this, regardless of domain):

```
✗  t-N  <action> with <invalid_input> rejected; with <valid_input>, succeeds
```

Split into separate items per assertion class. Two illustrative shapes from DIFFERENT domains so the rule is clearly about structure, not about a specific entity type — adapt to whatever your PR actually changes:

```
User-profile edit:
✗  t-N    Edit profile: missing email rejected; with email, save succeeds
✓  t-Na   HAPPY:    edit profile with valid email → 200, email visible on detail reload
✓  t-Nb   NEGATIVE: edit profile with empty email → "Email is required" error
                    (error_type: validation)

Saved search filter:
✗  t-N    Save filter: name too long rejected; valid name saves
✓  t-Na   HAPPY:    save filter with 30-char name → 200, filter visible in saved-filters list
✓  t-Nb   NEGATIVE: save filter with 256-char name → "Name max 255 chars" error
                    (error_type: validation)
```

The orchestrator runs `plan_smells.py` against your plan at the approval gate; combined-phrasing items get flagged with `⚠ <id>: combines happy and negative phrasing` for the human reviewer to catch. Don't make it work for that — write them split from the start.

## Write-operation persistence — REQUIRED separate item (v0.3.30+)

For any happy-path test item that performs a write (POST/PUT/DELETE, form submit, file upload, state-changing button click), the immediate response (toast / 200 / element visible) is necessary but NOT sufficient. The reviewer needs to know the change actually persisted AND that it round-trips correctly through the read path.

**Pre-v0.3.30 rule said "append persistence checks to `how:`". That was too soft** — planners shipped items with the round-trip step buried inside an already-long `how:` and skimped on it. The orchestrator's `plan_smells.py` would not have caught the omission since the check WAS technically there.

**v0.3.30+ rule: create a SEPARATE sibling item linked by `data_from`.** The save item is one test (does the submit-and-accept path work). The round-trip item is a SECOND test (does the just-written record load correctly through the read path). Two items, two pass/fail signals:

```jsonc
{
  "id": "t-005",
  "journey_id": "j-create-<resource>",
  "what": "HAPPY: create <Resource> 'fixture-1' with all required fields valid",
  "how": "Open /<list_route>/new; fill required fields including name='fixture-1'; Save; assert success toast and URL changes to /<list_route>/<id>.",
  "produces": ["created_id", "detail_url"],
  "tool": "chrome-devtools",
  "category": "frontend",
  "risk": "high",
  "depends_on": []
},
{
  "id": "t-006",
  "journey_id": "j-create-<resource>",
  "what": "HAPPY: re-open saved <Resource> — all fields round-trip correctly",
  "preconditions": "t-005 created a record at {{t-005.detail_url}}.",
  "how": "Navigate away to /<list_route> (the list page); assert the new row appears with name 'fixture-1'; click into the record; HARD-RELOAD the detail page; assert every submitted field renders with the submitted value — i.e. every field round-trips through the read path.",
  "tool": "chrome-devtools",
  "category": "frontend",
  "risk": "high",
  "depends_on": ["t-005"],
  "data_from": ["t-005"]
}
```

Why a separate item and not a bigger `how:`:

- **Two distinct pass/fail signals.** When save passes but round-trip fails, the report says "save: ✓, round-trip: ✗" and the reviewer immediately knows the bug is in the read path or the persistence layer — not in the form submission. Bundled into one item, the same scenario shows "fail" with a paragraph of mixed evidence.
- **`data_from` makes the dependency explicit.** Save fails → round-trip skips with `data-dep-failed: t-005`, not "fail for reasons that look like the read path is broken when actually the write never happened".
- **The orchestrator's `plan_smells.py` can mechanically check it.** Write items without a sibling reload-and-verify item via `data_from` get flagged at the approval gate. (You CAN'T mechanically check that the inline `how:` actually does persistence — only a separate item is detectable.)

**Apply this rule to**: CREATE, UPDATE, BULK-EDIT, IMPORT, RESTORE-FROM-VERSION-HISTORY, PUBLISH, ARCHIVE — any action that changes server state and is meant to be readable afterward.

**Skip this rule for**:
- DELETE (the "round-trip" is "not visible in list" — typically already covered by the original happy-path's "row disappears" assertion).
- Pure validation rejects (nothing was persisted to round-trip — the diff doesn't change the read path).
- API contract tests using `tool: "bash"` or `tool: "curl"` (the test suite is already exercising both write and read).
- `tool: "lint-only"` items (no execution at all — UI round-trip is meaningless).

**Sibling phrasing**: the orchestrator's lint looks for words like `re-open`, `round-trip`, `reload`, `re-render`, `loads back`, `navigates back`, `detail page`, `appears in list`, `visible in list`. Use one of these in the sibling item's `what:` so the lint recognizes the pattern and doesn't false-warn.

## Preconditions: don't bury the starting state in `how:`

When a test requires a specific starting state (logged-in role, seeded data, no existing record with conflicting name, feature flag enabled, etc.), put it in a separate `preconditions` field, NOT mixed into `how:`. Keeps the action steps focused; keeps it obvious to the executor what to verify-or-establish before the test action begins.

```jsonc
{
  "id": "t-005",
  "what": "Editing an existing <Record> updates <field>",
  "preconditions": "Logged in as <role>. A <Record> named 'fixture-edit-test' already exists (created by t-004 or via seed).",
  "how": "Navigate to /<list_route>; find the 'fixture-edit-test' row; click Edit; change <field>; click Save; assert the new value displays.",
  "tool": "chrome-devtools",
  "category": "api",
  "risk": "high",
  "depends_on": ["t-004"],
  "rationale": "..."
}
```

Don't write `preconditions` for items where the only precondition is "PRoctor is logged in" (that's covered by the auth flow). Use it when there's a non-trivial state that must exist for the test to be meaningful.

### Active precondition verification (`verify_precondition_via`, v0.3.29+)

`preconditions` as a string is descriptive — the executor renders it for human reviewers but doesn't enforce it. When the precondition is something the executor can CHECK cheaply (e.g. "DB has at least one published category", "feature flag X is enabled"), pair the description with an optional one-liner shell command:

```jsonc
{
  "id": "t-007",
  "what": "HAPPY: editing an existing category updates the slug",
  "preconditions": "At least one Category record exists in the DB.",
  "verify_precondition_via": "curl -sf \"$BASE_URL/api/categories?per_page=1\" | jq -e '.total > 0'",
  "how": "Navigate to /admin/categories; click first row; edit slug; save; assert toast.",
  "tool": "chrome-devtools",
  ...
}
```

Executor behavior (see `executing-pr-tests/SKILL.md`):
- Run the command via Bash. Exit 0 → proceed; non-zero → mark this item `skipped` with `reason: "precondition-not-met"` and DON'T dispatch the subagent.
- Templates (`{{<id>.<key>}}`) work inside the command just like in `how:` / `preconditions`.
- The command should be a SINGLE-SHOT CHECK, not a setup script. If the precondition needs to be ESTABLISHED rather than verified, put that in the consumer's setup flow (`.proctor/config.yml setup:` or a seed step earlier in the journey), not here.

When to add it:
- The test will give confusing results if the precondition isn't met (the assertion path runs against absent state and either passes vacuously or fails for the wrong reason).
- The check is cheap (≤ 1 second) and doesn't have side effects.
- The check has a clean exit-code signal — don't try to parse fuzzy stdout patterns.

When NOT to add it:
- The precondition is "logged in as developer" — already covered by auth flow.
- The check would cost more than the test itself (e.g. spinning up a fresh DB).
- The precondition is data this item itself creates as part of `produces` — that's `data_from` territory, not `verify_precondition_via`.

Distinct from `data_from`:
- `data_from: ["t-005"]` skips this item when an intra-run upstream item failed/skipped.
- `verify_precondition_via: "..."` skips this item when the ENVIRONMENT doesn't match. Different cause, different mitigation (rerun PRoctor with seeded data vs. fix the upstream test).

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

### Diff-pattern triggers for `error_type` (v0.3.27+)

The bias of unaided inference is to over-emit `validation` (validators are easy to spot in a diff) and under-emit the rest — especially `state-conflict`, which manifests as small schema/code patterns that don't visually scream "error handling". Run this helper against each hunk's added-lines BEFORE writing negative items:

```bash
git diff "$BASE_SHA" "$HEAD_SHA" -- "<hunk_file>" \
  | grep '^+' \
  | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/error_signals.py
```

Output is a JSON map `{error_type: [signal_names]}` — e.g. `{"state-conflict": ["gorm-unique-index-tag", "version-field-added"]}`. For every error_type the helper flagged, **plan at least one matching negative item**. The signal name goes into the item's `rationale` so reviewers can trace WHY the planner chose that error_type.

Below: what each pattern looks like in code, and the canonical test it should produce. Treat this as the planner's "if you see X in the diff, you owe an item of type Y" lookup.

**`state-conflict`** (the hardest type to spot, hence the most detail):

| Diff pattern | Helper signal | Plan this test |
|---|---|---|
| `CREATE UNIQUE INDEX ...` migration | `unique-index-added` | Create record A; try to create record B with same key → expect 409/422 with conflict message |
| `ADD CONSTRAINT ... UNIQUE` | `unique-constraint-added` | Same as above; verify the *exact* error message references the duplicate column |
| `gorm:"uniqueIndex"` struct tag | `gorm-unique-index-tag` | Submit form twice with same value → second submit rejected |
| `Version int` / `Revision int` field added | `version-field-added` | Load record; modify it from another session/tab; PUT with stale version → expect 409 |
| `WHERE version = ?` in update SQL | `version-where-clause` | Same as version-field-added; verify the update only affects 0 rows when version is stale |
| `StatusConflict` / `ErrConflict` / `409` literal | `conflict-response` | If the diff just added the response — exercise the code path that returns it |
| `"already exists" / "duplicate"` in error strings | `duplicate-error-returned` | Same — exercise that branch with a real duplicate |
| `if order.Status != "draft"` guard | `state-guard` | Set status to a non-draft value (publish/archive); attempt the guarded action → expect rejection |
| `SELECT ... FOR UPDATE` | `select-for-update` | Concurrent updates against same row — verify only one wins (best-effort; can be approximated with sequential edits in two browser contexts) |
| `sync.Mutex` / `sync.RWMutex` added | `sync-mutex` | If user-visible: trigger two concurrent requests; verify only one effect occurs |
| `idempotency_key` field / header | `idempotency-key` | Submit same request twice with same key; verify only ONE record created |

**`permission`** — role-based access:
- `if !user.IsAdmin` / `RequireRole(...)` / `policy.Allow(...)` / `StatusForbidden` returned
- Test: switch `as_account` to the lower-privilege role; attempt the action; assert 403 (or hidden UI element)

**`auth`** — session / authentication:
- `RequireAuth` middleware added; CSRF token check; `StatusUnauthorized` returned; session expiry logic
- Test: clear cookies / use expired token; attempt; assert redirect to login or 401

**`not-found`** — missing record:
- `gorm.ErrRecordNotFound` / `if x == nil { return ErrNotFound }` / `StatusNotFound` returned / `render :not_found`
- Test: hit URL with non-existent ID; assert 404 + correct empty-state UI (not 500)

**`network`** — external dependency failure:
- `http.Get/Post` / `Faraday` / `requests.get` / `retry` / `context.WithTimeout` / `CircuitBreaker`
- Test: hard to simulate in PRoctor without mocking — usually `lint-only` checking the error path EXISTS in source; or chrome-devtools blocking the URL via DevTools network override if the diff is end-user visible

**`validation`** — form / input rejection (the easy default; only plan when the helper actually flagged it):
- `validate(...)` call / `validate:"required"` tag / `ValidationError` / `400` returned / `yup.string().required()`
- Test: submit form with missing / malformed / out-of-range value; assert specific field-level error message

**When the helper returned NOTHING** but you still think a negative item makes sense — fine, infer one, leave `error_type` unset, and explain in `rationale` why the helper missed it. That feedback loop helps tune the patterns over time.

## Role-aware planning (when `.proctor/config.yml` has `auth.accounts`)

If the consumer's `.proctor/config.yml` declares an `auth` block with an `accounts` array, this admin has role-based permissions and you can target specific roles per item.

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

## Complex-form save items: cite the validator path in `how:` (v0.3.39+)

For chrome-devtools items that submit a form with multiple required fields (every `HAPPY: save / create / update` on a non-trivial form), the executor will read the validator source to enumerate required fields BEFORE filling and saving — that's the executor's "no whack-a-mole" rule. To make that work, **the planner must name the validator file in the item's `how:`** so the executor doesn't have to guess.

Mechanics:
- Walk the ChangeMap hunks for the file that contains the validator. Most likely candidates: paths matching `*_validator.go`, `models/<resource>.go`, `app/models/<resource>.rb`, `admin_resource.go` / `*_admin.go`, or any hunk whose `summary:` mentions "validator" / "validates" / "required". The `error_signals.py` helper run during planning surfaces these too (`error_type: validation` signals come from this code).
- In the item's `how:`, include an explicit reference, e.g.:

  > "Navigate to /<list_route>/new. Read the validator source at `<path-to-validator>` to enumerate every required field (note any type-driven branches, e.g. `case TypeImage` requiring asset). Fill ALL required fields with valid values in one pass. Click Save once."

- Don't paraphrase the required-field list into `how:` yourself. Cite the path; let the executor read fresh. The validator might have changed between planning and execution (rare but happens with auto-fix loops).

The executor's contract (in `agents/pr-test-executor.md` "no whack-a-mole" section) makes this load-bearing: if the path isn't cited and the executor can't find the validator, it falls back to DOM-snapshot heuristics which catch fewer requirements. So citing it isn't a nice-to-have — it's the input the executor needs to do its job correctly.

### Test-data convention in `how:` (v0.3.40+)

When `how:` suggests specific values for a happy save (a name, an email, a URL), use the `ai-test-` prefix convention rather than `fixture-1` / `test` / lorem ipsum. The executor enforces this contract too, but having the planner write it in `how:` upfront prevents the executor from inventing inconsistent markers:

- Name / title / slug: `ai-test-<resource>-<short-item-id>` — e.g. `"ai-test-image-reward-t007"`. The item-id suffix keeps two runs distinguishable.
- Email: `ai-test+<item-id>@proctor.example.com`.
- URL: `https://ai-test.example.invalid/<slug>` (the `.invalid` TLD is reserved).
- Description: `AI test record created by PRoctor item <item-id>`.
- Price / amount: an obvious outside-real-range value (`99999.99`, `0.01`).

Why: records created by PRoctor end up in shared dev / staging databases. `Reward "test"` is ambiguous; `Reward "ai-test-image-reward-t007"` is unmistakably from a PRoctor run and safe to GC. The convention also lets the human reviewer grep the DB for `ai-test-` to find every PRoctor-created record from any run.

## Coverage worksheet — write it BEFORE returning (v0.7.6+, mandatory)

Before running the plan_smells lint as the final step, build a coverage matrix that proves each load-bearing input has at least one item exercising it. This is the planner's self-audit — it forces you to look at every input the analyzer surfaced and match it to a plan item, instead of writing the plan from the diff alone and silently dropping requirements from PR comments / linked Jira tickets / new symbols introduced by the diff.

Walk these inputs IN ORDER:

1. **`pr_context.requirement_hints[]`** — each bullet is a documented acceptance criterion. For each entry, identify which item(s) cover it. If none, add an item before returning.
2. **`pr_context.linked_content[].excerpt`** — when `fetched: true`, scan the excerpt for testable criteria (lines starting with `- [ ]`, `must`, `should`, `verify that`, decision statements like "Decision: ..."). For each, identify which item(s) cover it.
3. **`pr_context.comments[].body`** — PR-author and reviewer comments often add scope after the PR body was written. For each comment that introduces a NEW criterion (not just "LGTM" / "thanks"), identify which item(s) cover it.
4. **Each new top-level symbol/function/method in the diff** — scan added lines for `+func <Name>(`, `+def <Name>(`, `+export function <Name>(`, `+export class <Name>(`, `+export const <Name>`. For each, identify which item EXERCISES (not just lints) it. `lint-only` items count as `lint_only`; a `chrome-devtools` / `bash` / `curl` item that actually CALLS the symbol counts as `exercised_by`.
5. **Each new branch in a validator** — when the diff adds a new `if` / `case` / `switch` branch in a validator file (heuristically files matching `*_validator.*` / `models/<resource>.*` / `*_validation.*`), identify which item triggers it.

Write the worksheet into a `planner_coverage_audit` top-level field of `test-plan.json` (the schema accepts this as an optional field):

```jsonc
{
  "planner_coverage_audit": {
    "by_pr_body": [
      { "criterion": "display_name capped at 100 chars", "covered_by": ["t-005"] }
    ],
    "by_linked_content": [
      { "source": "Jira PROJ-42",
        "criterion": "trim instead of reject for backward compat",
        "covered_by": ["t-006"] }
    ],
    "by_comments": [
      { "source": "PR comment by @alice 2026-05-12",
        "criterion": "enforce 100-char cap at API-level not just form",
        "covered_by": ["t-007"] }
    ],
    "by_diff_symbols": [
      { "symbol": "splitTags",
        "exercised_by": ["t-005", "t-006"],
        "lint_only": [] },
      { "symbol": "TrimDisplayName",
        "exercised_by": ["t-007"],
        "lint_only": ["t-002"] }
    ],
    "gaps": []
  },
  "journeys": [...],
  "items": [...]
}
```

**Gaps handling:**
- If you finish the walk and discover a row has no covering item, ADD AN ITEM before returning. Don't leave the gap in the worksheet and return — the worksheet's job is to force the add.
- If you genuinely can't add an item (e.g. the new symbol is a private helper with no callable surface; the criterion is "log message wording" that's neither lint-checkable nor runtime-observable), leave the row in `gaps` with a short explanation. Example: `{"input": "private helper foo()", "why_no_item": "Internal helper; tested transitively via t-005"}`. The plan_smells lint downstream (Fix C) reads `gaps` and won't double-report inputs the planner explicitly excused.
- Empty `gaps: []` means clean — every input is covered.

This worksheet is part of the contract — schema accepts it as optional, but the planner skill SHOULD always emit it. Reviewers reading the plan can scan the worksheet to confirm the planner saw the same inputs they did.

## Self-audit BEFORE handing the plan back (v0.3.35+)

After writing `test-plan.json` and BEFORE returning to the orchestrator, you MUST run the plan-smells lint as the LAST step of this skill. This is the safety net for everything in this skill — every rule above (one-assertion-per-item, write-needs-roundtrip, coverage balance) is mechanically checked here:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/plan_smells.py --strict \
    --change-map .proctor/runs/<run-id>/change-map.json \
    --diff .proctor/runs/<run-id>/diff.patch \
    < .proctor/runs/<run-id>/test-plan.json
```

The `--change-map` and `--diff` flags (v0.7.6+) enable the additional `pr-body-coverage` and `new-symbol-not-exercised` checks. (The v0.7.7–v0.7.10 `--setup-context` flag is still parsed for CLI compat but ignored — its `missing-runtime-verify-when-supplementary-binary-present` rule was removed in v0.7.11.) When `--change-map` / `--diff` are absent (legacy runs), the corresponding check silently no-ops and only the v0.7.5 plan-internal lints run.

- **Exit 0** → plan is clean. Print `[proctor:plan] done — N items planned, plan_smells clean` and return.
- **Exit 1** → READ the warnings on stdout. Each warning tells you exactly what's wrong: items combining happy+negative phrasing, write actions without a `data_from` sibling doing round-trip verification, or 2+ negative items with zero happy-path saves (the "all-negative plan" coverage gap). Regenerate the plan addressing every warning:
  - Split combined items per assertion class.
  - Add round-trip sibling items linked by `data_from` for every save/create.
  - If you skipped happy-path saves because of "backend dep deferred" or similar reasoning — STOP. That's the exact failure mode the lint exists to catch. Plan the happy save anyway, using `tool: "skip"` with `rationale: "backend dependency <ref> not yet deployed — surfacing the planned step so it's visible in the report"` if you genuinely can't run it. The plan must NOT silently omit the happy path.
  - Overwrite test-plan.json with the regenerated plan.
  - Re-validate via `schema.validate_test_plan`.
  - Re-run the lint. Loop max 2 times.
- After 2 failed regen attempts, give up gracefully: write a `[proctor:plan] WARNING: plan_smells still emits warnings after 2 regens; surfacing as-is for human review` line, leave the plan as-is, and return. The orchestrator's downstream advisory mode will print the warnings before the approval gate.

**Do NOT** skip this self-audit. The orchestrator's approval gate used to do it (`6c-lint` / `6d` in earlier versions) but the controlling AI repeatedly skipped that step. Putting the lint INSIDE this skill — at the contract boundary, alongside schema validation — makes it impossible to skip without abandoning the skill mid-execution.

**Do NOT** rationalize that "this PR's nature makes happy-path tests impossible". Validators only check what saves; if you only test what gets rejected, you're testing the cage, not the building. The lint's "all-negative plan" warning fires specifically to catch this rationalization.

## Constraints

- Emit exactly one JSON object. No prose.
- IDs must be unique. `depends_on` must reference IDs that exist in the same plan.
- `tool` must be one of: `chrome-devtools`, `bash`, `curl`, `lint-only`, `skip`.
- When set, `as_account` must equal one of `auth.accounts[].name` values from `.proctor/config.yml`. The validator rejects unknown names.
- The final test-plan.json MUST pass `scripts/plan_smells.py --strict` (or 2 failed regen attempts, with the warning explicitly surfaced).

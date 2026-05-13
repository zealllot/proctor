---
name: analyzing-pr-changes
description: Use when /proctor needs to convert a GitHub PR (metadata + diff) into a structured ChangeMap categorizing every changed hunk. First stage of the PRoctor pipeline. Output is a single JSON object — no prose. Use when you see "apply skill analyzing-pr-changes" or when the orchestrator hands you `pr.json` + `diff.patch`.
---

# Analyzing PR Changes

Input: GitHub PR metadata (the JSON from `gh pr view --json ...`) and the
unified diff (the output of `gh pr diff`).

Output: a single JSON object matching the `ChangeMap` contract — emit it
on stdout with no surrounding prose, headings, or code fences.

## Procedure

1. Extract identity from the PR JSON:
   - `pr.number`, `pr.head_sha` (= `headRefOid`), `pr.base_sha` (= `baseRefOid`), `pr.url`.

2. Extract `pr_context` from the PR JSON for the planner to use later:
   - `title`: the PR title (from `title` in pr.json).
   - `body`: the full PR description body (from `body` in pr.json), or the empty string if absent. Preserve markdown.
   - `links`: deduplicated list of HTTP/HTTPS URLs found in the body. This explicitly INCLUDES Slack permalinks (`*.slack.com/archives/...`), Jira/Atlassian tickets (e.g. `*.atlassian.net/browse/PROJ-123` or `*.atlassian.net/wiki/...`), Linear / Notion / Confluence / Figma / Loom / GitHub URLs, and any other links the author dropped. The planner uses these as evidence that there's a documented requirement to verify against.
   - `requirement_hints`: short list of bullet snippets extracted from the body that look like acceptance criteria (lines starting with `- [ ]`, headings like "## Requirements" / "## AC", numbered lists under "must" / "should"). At most 8 entries; empty list if nothing matches. Cap each entry at ~120 chars.
   - `directives`: machine-readable user overrides. Look for HTML comments in the body of the form `<!-- proctor:<key> <value> -->`. Recognized keys:
     - `<!-- proctor:skip-paths vendor/ third_party/ generated/** -->` → `directives.skip_paths` is a list of glob patterns; hunks whose `file` matches any pattern are dropped from `hunks` before classification (and won't trigger any test items downstream).
     - `<!-- proctor:skip-categories docs cli -->` → `directives.skip_categories` is a list; after classifying each hunk, drop hunks whose category appears here.
     - `<!-- proctor:focus-paths src/payments/ -->` → `directives.focus_paths` is a list of glob patterns; if non-empty, KEEP only hunks whose file matches at least one pattern. Applied AFTER skip_paths.
     - `<!-- proctor:max-items 5 -->` → `directives.max_items` is an int the planner will respect as a soft cap on item count.
     Omit `directives` from `pr_context` entirely if no recognized comment is present. Unknown keys are silently dropped.

   This step is purely textual — do not follow the URLs and do not fetch anything external. Just record them.

3. **Apply path directives BEFORE classifying.** If `pr_context.directives.skip_paths` is non-empty, drop any hunk whose `file` matches one of those globs. If `directives.focus_paths` is non-empty, keep only hunks matching at least one focus glob. The remaining hunks are what gets classified.

4. Walk the diff. For every changed file, decide its category by these
   rules (apply in order; first match wins):

   | Pattern | Category |
   |---|---|
   | path `^docs/`, `*.md`, comment-only changes | `docs` |
   | path `^migrations/`, `*.sql`, ORM models | `schema` |
   | path `Dockerfile`, `docker-compose*`, `^.github/workflows/` | `infra` |
   | path `^ios/`, `^android/`, `*.swift`, `*.kt`, RN screen files | `mobile` |
   | path `^cmd/`, `^bin/`, file is an executable entrypoint, `*_cmd.go` | `cli` |
   | extension `.tsx`/`.jsx`/`.vue`/`.svelte`/`.css`/`.scss`/`.html` | `frontend` |
   | path resembles a backend handler (`*_handler.go`, `*Controller.*`, route definition) | `api` |
   | anything else with code changes | `api` (default for backend code) |

5. For each hunk, also assign:
   - `risk`: `low` (cosmetic, comments, isolated additions), `medium`
     (logic change but localized), `high` (touches auth, payments, data
     migrations, public API contracts, critical path).
   - `summary`: one sentence describing the change in plain English.

6. **Apply category directives.** If `pr_context.directives.skip_categories` is non-empty, drop any hunk whose category appears in that list.

7. **Compute `impact_radius` per non-docs hunk** (v0.3.24+). For each
   surviving hunk, find the callers of the identifiers it changed so
   the planner can plan regression coverage proportional to blast
   radius. Skip this entirely for `category: "docs"` hunks (no
   importers worth tracing). The procedure:

   a. **Extract candidate identifiers from the hunk's added/modified
      lines.** Use language-aware heuristics:
      - **Go**: exported identifiers (capitalized) on lines starting
        with `func`, `type`, `var`, `const` — including methods
        (`func (r *Receiver) Name(`). Skip lowercase / unexported.
      - **TypeScript / JavaScript**: identifiers in `export function`,
        `export class`, `export const`, `export default function`,
        `export type`, `export interface`, named exports
        (`export { Foo, Bar }`). Also default-exports' inferred name.
      - **Python**: top-level `def NAME(` and `class NAME(` lines —
        skip names starting with `_` (private convention).
      - **Ruby**: `def NAME`, `class NAME`, `module NAME`. Skip private
        sections if you can see a `private` marker above.
      - **Other languages**: best-effort by file extension; if you
        cannot reliably extract identifiers, OMIT `impact_radius` for
        that hunk (it's optional — better than wrong).
      Cap the candidate list at the top 6 identifiers per hunk to keep
      grep cost bounded.

   b. **Delegate the grep + threshold + ranking to the helper script.**
      Don't hand-roll the aggregation — call the dedicated script
      which has tested filter rules (single-match files dropped, test
      / vendor / build paths excluded, sorted by descending count,
      capped at top 10):

      ```bash
      python3 ${CLAUDE_PLUGIN_ROOT}/scripts/impact_radius.py \
          --file <FILE_THAT_CHANGED> \
          --idents "Ident1 Ident2 Ident3" \
          --repo .
      ```

      Output is a JSON object `{"files": [...], "truncated": <bool>}`.
      Plug `files` into the hunk's `impact_radius` field; plug
      `truncated` (when `true`) into `impact_radius_truncated` so the
      planner knows the visible 10 don't represent the full radius
      and can auto-upgrade the hunk's risk to `high`.

      v0.3.28+: when `truncated` is `true`, **also override this
      hunk's `risk` field to `"high"`** before emitting. The planner
      reads risk and plans more aggressively for high; a hunk whose
      visible fan-out is capped at 10 but actually has 100s of
      callers shouldn't sit at the original (likely `medium`) risk
      just because the visible list happens to fit.

   c. **What the script enforces** (you don't need to replicate this —
      just know what's filtered for you):
      - Word-boundary regex per identifier (`\bIdent\b` — no
        substring matches).
      - Excludes the changed file itself, plus `*_test.{go,ts,tsx,js,jsx}`,
        `*.spec.*`, `tests/`, `test/`, `__tests__/`, `vendor/`,
        `node_modules/`, `dist/`, `build/`, `target/`, `.proctor/`.
      - **Threshold: cumulative count per caller must be ≥ 2**
        (v0.3.26+). A single match is overwhelmingly just an
        `import { Foo } from '...'` line or a re-export, NOT a
        real caller. Filtering single-matches drops the most common
        kind of regex-based false positive (a name that's *imported*
        but never *used*).
      - Sort by descending count, then path ascending for stability.
      - Cap at top 10 entries.

   d. **Output handling.** The script may return:
      - **Non-empty list** → emit as `"impact_radius": [...]`.
      - **Empty list** (analyzed, nothing crossed threshold) → emit
        `"impact_radius": []`. This is distinct from "didn't analyze"
        and tells the planner not to plan regression items for this
        hunk.
      - **Script failure** (no git, hunk in an untracked path, etc.) →
        OMIT the field entirely. The planner falls back to legacy
        behavior (no impact-aware planning) for that hunk.

   e. **What this is NOT.** This is a grep + frequency threshold, not
      a compiler-grade import resolver. False positives where two
      unrelated symbols share an identifier name AND the unrelated
      file uses the colliding name ≥ 2 times will still slip through.
      The planner treats `impact_radius` as a hint ("these files
      MIGHT regress"), not a fact ("these files WILL regress"). Don't
      try to be precise here — be cheap, fast, and language-agnostic.

8. Compute `categories_present` as the deduplicated set of hunk
   categories (after directive filters in steps 3 and 6).

9. **Cross-cutting**: if both `frontend` and `api` appear among
   `categories_present`, the `e2e-flow` category will be added by the
   *next* stage (planner), not here. Do not invent it now.

## Output JSON shape

```jsonc
{
  "pr": { "number": 0, "head_sha": "...", "base_sha": "...", "url": "..." },
  "pr_context": {
    "title": "...",
    "body": "...",
    "links": ["https://acme.atlassian.net/browse/PROJ-42", "https://acme.slack.com/archives/C0/p123"],
    "requirement_hints": ["display name max length 100", "rate limit endpoint at 60/min"]
  },
  "hunks": [
    {
      "file": "admin/rewards/handler.go",
      "category": "api",
      "risk": "high",
      "summary": "Add type=Image / type=Game branches in CreateReward.",
      "impact_radius": [
        "admin/rewards/router.go",
        "admin/dashboards/rewards_widget.go"
      ],
      "impact_radius_truncated": false
    }
  ],
  "categories_present": ["api"]
}
```

## Constraints

- Emit exactly one JSON object. No markdown fences, no extra prose.
- Use only these categories: `frontend`, `api`, `schema`, `infra`, `mobile`, `cli`, `docs`. (`e2e-flow` is added by the planner.)
- Use only these risks: `low`, `medium`, `high`.

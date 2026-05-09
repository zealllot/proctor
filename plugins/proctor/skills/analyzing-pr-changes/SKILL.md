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

7. Compute `categories_present` as the deduplicated set of hunk
   categories (after directive filters in steps 3 and 6).

8. **Cross-cutting**: if both `frontend` and `api` appear among
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
    { "file": "...", "category": "frontend", "risk": "low", "summary": "..." }
  ],
  "categories_present": ["frontend"]
}
```

## Constraints

- Emit exactly one JSON object. No markdown fences, no extra prose.
- Use only these categories: `frontend`, `api`, `schema`, `infra`, `mobile`, `cli`, `docs`. (`e2e-flow` is added by the planner.)
- Use only these risks: `low`, `medium`, `high`.

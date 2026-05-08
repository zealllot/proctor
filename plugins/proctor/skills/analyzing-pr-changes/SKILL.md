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

2. Walk the diff. For every changed file, decide its category by these
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

3. For each hunk, also assign:
   - `risk`: `low` (cosmetic, comments, isolated additions), `medium`
     (logic change but localized), `high` (touches auth, payments, data
     migrations, public API contracts, critical path).
   - `summary`: one sentence describing the change in plain English.

4. Compute `categories_present` as the deduplicated set of hunk
   categories.

5. **Cross-cutting**: if both `frontend` and `api` appear among
   `categories_present`, the `e2e-flow` category will be added by the
   *next* stage (planner), not here. Do not invent it now.

## Output JSON shape

```jsonc
{
  "pr": { "number": 0, "head_sha": "...", "base_sha": "...", "url": "..." },
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

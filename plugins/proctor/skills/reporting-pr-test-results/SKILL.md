---
name: reporting-pr-test-results
description: "Final stage of /proctor. Renders TestResults + FixPRRef into a markdown report. In CI mode (PROCTOR_POST_COMMENT=1) posts the comment to the PR; in local mode saves the report to disk and prints the path so the developer can read it locally. Each item gets its own section with evidence, command, output excerpt, logs, and screenshot refs (when present)."
---

# Reporting PR Test Results

Input: `test-results.json`, `fix-pr-ref.json` (may be `null`), `change-map.json`, plus environment variables passed in by the orchestrator:

- `PR`, `REPO` — for cross-linking to the PR itself
- `RUN_ID` — PRoctor's run-id (the path component under `.proctor/runs/`)
- `GITHUB_RUN_ID` — the GitHub Actions workflow run id, used to build the Action run URL and the artifact link
- `GITHUB_SERVER_URL` — usually `https://github.com`
- `SCREENSHOT_URL_BASE` — when present, screenshots have been pushed to a public branch and the report should inline-embed them via this URL prefix (e.g. `https://raw.githubusercontent.com/<owner>/<repo>/proctor-screenshots/<run-id>/`). When absent, fall back to a "(in artifact)" reference.
- `VISUAL_URL_BASE` — when non-empty, a public raw-URL prefix for `baseline.png` / `head.png` / `diff.png`. Render a "Visual regression" section right after the header table (see template below).
- `PROCTOR_VISUAL_DIFF_PIXELS` — number of differing pixels at 5% fuzz. `0` means no visible change. Echo this in the visual section as a one-line summary.
- `PROCTOR_USAGE_SUMMARY` — when present, a string like `tokens_in=45000 tokens_out=8000 cost_usd=0.1234|analyze=$0.020(1) plan=$0.030(1) execute=$0.080(5) execute-lint-batch=$0.012(1) report=$0.014(1)` summarizing total claude API usage for this run, plus a per-stage breakdown (after the `|`). Each per-stage entry is `<stage>=$<cost>(<calls>)`. Render two lines in the header:
  - **Cost:** $<total> · <tokens_in> in / <tokens_out> out tokens
  - **Where:** <stage>=$<cost> · <stage>=$<cost> · ... (sorted by cost desc when possible; top 5 only if there are more)
  Skip both lines when empty.

Output: a markdown comment body. The skill posts the comment via `scripts/post_comment.py`.

## Header

```markdown
## PRoctor report — PR #<num> @ <head_sha[:7]>

**Summary:** <pass_emoji> <pass>/<total> passed · <fail> failed · <skipped> skipped
**Run:** [Action #<github-run-id>](<server>/<repo>/actions/runs/<github-run-id>) · [download artifacts](<server>/<repo>/actions/runs/<github-run-id>#artifacts)
**Run id:** `<run-id>`
**Cost:** $<cost_usd> · <tokens_in> in / <tokens_out> out tokens   ← only when PROCTOR_USAGE_SUMMARY is non-empty
```

The `<server>/<repo>/actions/runs/<github-run-id>#artifacts` URL takes the user straight to the artifacts panel where they can download `proctor-run-<pr#>.zip` containing every JSON, log, and screenshot.

## Visual regression section (only when `VISUAL_URL_BASE` is non-empty)

Render this immediately after the header, before the per-item sections:

```markdown
### Visual regression — `<base_url>`

`<diff_pixels>` differing pixels at 5% fuzz (0 = no visible change).

| Baseline (base ref) | Diff (red = changed) | Head (this PR) |
|---|---|---|
| ![baseline](VISUAL_URL_BASE/baseline.png) | ![diff](VISUAL_URL_BASE/diff.png) | ![head](VISUAL_URL_BASE/head.png) |

The base URL above is the `base_url` from `.pr-test.yml`. Captured by chromium headless at 1280×800. Pages with animations or randomized content may always show diff pixels — tune `.pr-test.yml.teardown` if your stack needs custom server cleanup between captures.
```

When `VISUAL_URL_BASE` is empty, omit the entire section — don't render an empty header.

## Journey grouping (v0.3.23+ loose, v0.3.28+ structured)

Group items by journey for the report.

**Structured form (v0.3.28+, preferred)**: when the plan has a top-level `journeys` array, items carry `journey_id` referencing `journeys[].id`. Look up the matching `{id, goal, terminal_state}` entry; the report header includes BOTH the goal (one sentence describing what the user accomplishes) AND the terminal_state (the assertable end-state) so reviewers can scan whether each journey met its bar:

```markdown
### Journey: Create-Image-Reward — 3/4 passed

**Goal:** Admin creates a published Image-type digital reward.
**Terminal state:** Reward appears in /admin/rewards list with status=Published and re-renders correctly after a hard reload.

<item rows for that journey, in plan order>
```

**Legacy form (v0.3.23, fallback)**: when items only have a free-form `journey: "<name>"` string, render the simpler header `### Journey: <name> — <pass>/<total>` without goal/terminal_state lines.

Order journeys by their position in the plan's `journeys` array (structured) or by first-appearance among items (legacy). After all journeys, append an "Other" section for items that don't carry a journey reference. Skip the "Other" section if it's empty.

Skipped items come in three flavors — render each distinctly so the reviewer doesn't lump them together:

- **`reason: "data-dep-failed: t-007"`** → `⏭ skipped (upstream t-007 failed)`. Test was INVALIDATED by an intra-run sibling. Reviewer should fix the upstream first.
- **`reason: "data-template-missing: t-007.created_id"`** → `⏭ skipped (upstream t-007 didn't produce created_id)`. Same family — producer broke its contract; reviewer jumps to t-007's row.
- **`reason: "precondition-not-met"`** (v0.3.29+) → `⚠ skipped (environment precondition failed)`. Different cause: the test's assumed starting state is missing — this is an ENVIRONMENT GAP, not a bug in the diff under test. Render the failing command and its exit code from `evidence` so the reviewer can rerun PRoctor against a properly seeded environment.

Don't render any of these collapsed identically to ordinary opt-out `skipped` items (`tool: "skip"` or `status: "skipped"` without a reason).

In the HTML report, journeys become `<details>` blocks that default open if the journey has any fail items, closed if all-pass. Same logic as per-item details but one level higher.

## Per-item section

For EACH item, render a `<details>`-collapsed block. Pass items default closed; fail items default open (`<details open>`); skipped items default closed.

```markdown
<details><summary>{status_emoji} <code>t-001</code> — {what} {category_chip}</summary>

**What it did:** {1–2 sentence plain-English description of the test action — read evidence + command for context}

**Evidence:** {item.evidence verbatim}

{if item.command}**Command:**
```bash
{item.command}
```
{end-if}

{if item.output_excerpt}**Output:**
```
{item.output_excerpt — truncate to 60 lines if longer, suffix with "... (truncated)"}
```
{end-if}

{if item.screenshot_ref}**Screenshot:**
{if SCREENSHOT_URL_BASE is set}
![{item.id} screenshot]({SCREENSHOT_URL_BASE}{basename of item.screenshot_ref})
{else if PROCTOR_POST_COMMENT == 0}
![{item.id} screenshot](file://{absolute path to item.screenshot_ref})

_Path: `{item.screenshot_ref}` (open in Preview / VS Code markdown
preview for inline rendering, or click the link above on macOS)._
{else}
[`{item.screenshot_ref}` in artifact](<server>/<repo>/actions/runs/<github-run-id>#artifacts)
{end-if}
{if item.screenshot_focus}_What to look for:_ {item.screenshot_focus}{end-if}
{end-if}

{if item.logs_ref}
{if PROCTOR_POST_COMMENT == 0}
**Full log:** `{absolute path to item.logs_ref}`
{else}
**Full log:** `{item.logs_ref}` (in artifact)
{end-if}
{end-if}

{if status == fail AND item.reason}**Failure reason:** `{item.reason}`
{end-if}

{if item.outputs is non-empty}**Captured artifacts** (consumed by downstream items via `{{<id>.<key>}}`):
```
{for each key, value in item.outputs:}
{key} = {value}
{end}
```
{end-if}

</details>
```

If the item has no `command` / `output_excerpt` / `screenshot_ref` / `outputs`, omit those subsections. The section must always include **What it did** and **Evidence**.

When an item was skipped because of a missing producer output (`reason: "data-template-missing: t-007.created_id"`), surface the upstream id + key in **Failure reason** so the reviewer can jump to the producer's row instead of inspecting the consumer's empty render. Producer-side, when an item failed because of `reason: "producer-missing-output"`, render the declared `produces:` keys vs. the actual `outputs:` keys side-by-side in the **Captured artifacts** subsection so the contract gap is obvious.

## Auto-fix section

CI mode (FixPRRef has `number` / `url` fields):

```markdown
### Auto-fix

{ONE of:}
- ✅ Opened fix PR: [#<num>](<server>/<repo>/pull/<num>) covering `<id1>`, `<id2>`. Review and merge if happy.
- ⚠️ Opened fix PR: [#<num>](...) covering `<id1>`. **Couldn't fix:** `<id2>` — needs human review.
- ⛔ Failures couldn't be auto-fixed. Needs human review.
- ⏸️ Auto-fix disabled (`.pr-test.yml` has `auto_fix: false`). See failures above.
- ✨ All passed — nothing to fix.
```

Local mode (FixPRRef has `mode: "local"` and `patches_dir`):

```markdown
### Auto-fix (local — patches not pushed)

{ONE of, depending on covers/unfixed:}
- 📝 Generated patches in `<patches_dir>` covering `<id1>`, `<id2>`. Apply with: `git apply --3way <patches_dir>/<id>.patch`
- 📝 Generated patches in `<patches_dir>` covering `<id1>`. **Couldn't fix:** `<id2>` — needs human review.
- ⛔ Failures couldn't be auto-fixed. Needs human review.
- ⏸️ Auto-fix disabled (`.pr-test.yml` has `auto_fix: false`). See failures above.
- ✨ All passed — nothing to fix.

Re-run with `--push-fix` to also push these as a fix PR.
```

## Procedure

1. Render the markdown above using actual values from the inputs.
2. For chrome-devtools items where the executor populated `screenshot_ref`, link to the artifact location (we don't have a public URL for inline rendering yet — see `docs/INTEGRATION.md` "Inline screenshots" if/when this becomes available).
3. Compute a one-line summary (`<pass>/<total> passed`) for use as the `summary_for_gist` if the body exceeds GitHub's comment size limit.
4. Save the rendered markdown to `.proctor/runs/<run-id>/report.md` (always — both modes need it).
5. Branch on `PROCTOR_POST_COMMENT`:
   - `1` (CI) → call `post_comment.post(pr_number=..., repo=..., body=<rendered>, summary_for_gist=<one-line>)`. Emit `[proctor:report] done comment_posted=true`. Done.
   - `0` (local) → **also generate an HTML report** alongside the markdown (markdown is for posting to PRs; HTML is the dev-facing artifact). See the HTML template below.

   For local mode, print to stdout in this exact order:
     1. Status line: `[proctor:report] done comment_posted=false`
     2. A short summary: `<pass>/<total> passed · <fail> failed · <skipped> skipped · cost $<n> · time <Nm>`
     3. The three follow-up lines:
        ```
        ──────
        HTML report:  <abs path to .proctor/runs/<run-id>/report.html>  ← OPEN THIS
        Markdown:     <abs path to .proctor/runs/<run-id>/report.md>
        Screenshots:  <abs path to .proctor/runs/<run-id>/screenshots/>
        Patches:      <abs path to .proctor/runs/<run-id>/patches/>      (only if any; otherwise "none")
        ```
     4. Try to `open <abs path to report.html>` (macOS) or `xdg-open <...>` (Linux); on failure (e.g. headless), just leave the path printed.

   In local mode, **do NOT dump the full markdown to chat**. The HTML report is the readable artifact, the markdown is a backup for PR-posting use. Dumping the markdown verbatim adds noise in the terminal that's worse than the HTML.

## HTML report template (local mode)

Generate `<run-dir>/report.html` as a single self-contained file. Screenshots referenced by relative path (`./screenshots/<id>.png`) so the file is small and portable as long as you don't move it out of the run dir.

Structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PRoctor — PR #<num> @ <head_sha[:7]></title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 960px; margin: 0 auto; padding: 24px; line-height: 1.5; }
    header { position: sticky; top: 0; background: Canvas; padding: 16px 0; border-bottom: 1px solid #444; }
    h1 { margin: 0; font-size: 1.4em; }
    .summary { display: flex; gap: 16px; margin: 8px 0; font-size: 0.9em; color: #888; }
    .summary .pass { color: #2da44e; }
    .summary .fail { color: #cf222e; }
    .item { border: 1px solid #ddd; border-radius: 6px; margin: 12px 0; }
    .item summary { padding: 12px; cursor: pointer; user-select: none; }
    .item.pass summary { color: #2da44e; }
    .item.fail summary { color: #cf222e; }
    .item.skip summary { color: #888; }
    .item-body { padding: 0 16px 16px; border-top: 1px solid #eee; }
    .section { margin: 12px 0; }
    .section h3 { font-size: 0.85em; text-transform: uppercase; color: #888; margin: 4px 0; letter-spacing: 0.05em; }
    .section p { margin: 0; }
    pre { background: #f6f8fa; padding: 8px 12px; border-radius: 4px; overflow-x: auto; font-size: 0.85em; }
    .screenshot { max-width: 100%; border: 1px solid #ddd; border-radius: 4px; margin-top: 8px; }
    .chip { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75em; background: #eee; color: #555; }
    @media (prefers-color-scheme: dark) {
      body { background: #0d1117; color: #c9d1d9; }
      .item { border-color: #30363d; }
      pre { background: #161b22; }
      .chip { background: #21262d; color: #8b949e; }
      .screenshot { border-color: #30363d; }
    }
  </style>
</head>
<body>
<header>
  <h1>PRoctor — PR #<num> @ <head_sha[:7]></h1>
  <div class="summary">
    <span class="pass">✓ <pass>/<total> passed</span>
    {if fail > 0}<span class="fail">✗ <fail> failed</span>{end-if}
    {if skipped > 0}<span>⏭ <skipped> skipped</span>{end-if}
    <span>Cost: $<cost></span>
    <span>Run id: <code><run-id></code></span>
  </div>
</header>

{for each item, ordered: failures first, then pass, then skipped:}
<details class="item <pass|fail|skip>" {open if fail}>
  <summary>
    <strong>[<status emoji>] <id></strong>
    — <what>
    <span class="chip"><category></span>
    {if as_account}<span class="chip">as: <as_account></span>{end-if}
  </summary>
  <div class="item-body">
    {if rationale}
    <div class="section">
      <h3>Why this test</h3>
      <p><rationale></p>
    </div>
    {end-if}

    <div class="section">
      <h3>What it did</h3>
      <p><1–2 sentence plain-English description of the test action></p>
    </div>

    {if command}
    <div class="section">
      <h3>How (the actual command)</h3>
      <pre><command></pre>
    </div>
    {end-if}

    <div class="section">
      <h3>Result</h3>
      <p><evidence>{if status==fail and reason}<br><strong>Failure reason:</strong> <reason></code>{end-if}</p>
      {if output_excerpt}<pre><output_excerpt — truncated to 60 lines></pre>{end-if}
    </div>

    {if screenshot_ref}
    <div class="section">
      <h3>Screenshot</h3>
      <img class="screenshot" src="./screenshots/<basename of screenshot_ref>" alt="<id> screenshot">
      {if screenshot_focus}<p><em>What to look for:</em> <screenshot_focus></p>{end-if}
    </div>
    {end-if}

    {if logs_ref}
    <div class="section">
      <h3>Full log</h3>
      <p><a href="<relative path to logs_ref>"><logs_ref></a></p>
    </div>
    {end-if}
  </div>
</details>
{end-for}

{if FixPRRef}
<section style="margin-top: 32px;">
  <h2>Auto-fix</h2>
  <!-- Same as markdown's Auto-fix section, just in HTML. -->
</section>
{end-if}

</body>
</html>
```

The HTML must be valid (no `</strong>` mismatched, etc.). Test for parse-ability before saving (Python `html.parser` minimal check). If parse fails, write the markdown as fallback and warn.

## Constraints

- Status emojis: ✅ pass · ❌ fail · ⏭ skipped.
- Category chip: render as a backtick-wrapped tag, e.g. `` `frontend` ``.
- Per-item sections must use `<details>` so the comment stays scannable but every detail is one click away.
- For fail items, default the `<details>` block to `open` so the human sees them immediately.
- Header summary line must use the dominant status emoji (✅ if 0 fails, ❌ if any fails, ⏭ if all skipped).
- When evidence or output_excerpt contains backticks, escape them (`` ``` `` → `~~~` for fenced blocks if needed) so the comment renders cleanly.
- If `output_excerpt` is over ~60 lines, truncate the middle (keep first 30 + last 20) with `... (truncated, see full log) ...` between.

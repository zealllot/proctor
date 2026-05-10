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
{else}
[`{item.screenshot_ref}` in artifact](<server>/<repo>/actions/runs/<github-run-id>#artifacts)
{end-if}
{if item.screenshot_focus}_What to look for:_ {item.screenshot_focus}{end-if}
{end-if}

{if item.logs_ref}**Full log:** `{item.logs_ref}` (in artifact)
{end-if}

{if status == fail AND item.reason}**Failure reason:** `{item.reason}`
{end-if}

</details>
```

If the item has no `command` / `output_excerpt` / `screenshot_ref`, omit those subsections. The section must always include **What it did** and **Evidence**.

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
   - `1` → call `post_comment.post(pr_number=..., repo=..., body=<rendered>, summary_for_gist=<one-line>)`. Emit `[proctor:report] done comment_posted=true`.
   - `0` → skip the post call. Print to stdout:
     ```
     [proctor:report] done comment_posted=false
     Report saved to .proctor/runs/<run-id>/report.md
     <pass>/<total> passed · <fail> failed · <skipped> skipped
     ```
     Then dump the full rendered markdown so the developer sees the full report inline. (The double-log concern from CI doesn't apply here — local invocations are interactive and the user explicitly wants to read the report.)

## Constraints

- Status emojis: ✅ pass · ❌ fail · ⏭ skipped.
- Category chip: render as a backtick-wrapped tag, e.g. `` `frontend` ``.
- Per-item sections must use `<details>` so the comment stays scannable but every detail is one click away.
- For fail items, default the `<details>` block to `open` so the human sees them immediately.
- Header summary line must use the dominant status emoji (✅ if 0 fails, ❌ if any fails, ⏭ if all skipped).
- When evidence or output_excerpt contains backticks, escape them (`` ``` `` → `~~~` for fenced blocks if needed) so the comment renders cleanly.
- If `output_excerpt` is over ~60 lines, truncate the middle (keep first 30 + last 20) with `... (truncated, see full log) ...` between.

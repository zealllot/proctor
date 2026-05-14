# Changelog

All notable changes to PRoctor are documented here. Versions follow semver: `v0.x.y` where `x` bumps on minor pipeline-affecting changes and `y` on action wrapper / packaging fixes.

## v0.6.8 — 2026-05-14

### Negative-test screenshot contract: error must be rendered in DOM, not just response body

The v0.6.6 e2e run against mcd-website PR #1115 (run-id `pr1115-e6a7c79-v066manual155241`) shipped t-007 / t-008 / t-009 — three negative-test items — with three byte-identical PNGs (244252 bytes each, the blank "Add Digital Content" form). Each item's evidence claimed an error chip rendered (`"Digital Content Type is required"` / `"Game URL is required"` / `"Game URL is not a valid URL"`); the user noticed the screenshots all looked the same and proved otherwise.

**Root cause**: the Pattern A submit step in `satisfying-form-preconditions/SKILL.md` used `fetch(form.action, ...)` — a programmatic POST. The server returns 422 + error HTML, the executor reads `await resp.text()` and observes the expected error string (empirical evidence the validator branch fired), but the **browser DOM never updates** because `fetch` is decoupled from page navigation. `take_screenshot` then captures the pre-submit form. For happy-save items this is fine (the evidence is the redirect URL); for negative items the asserted artifact IS the rendered error, and a fetch-only submit never renders one.

**Fix**:

- **Skill update** (`skills/satisfying-form-preconditions/SKILL.md`): new section *"Negative-test screenshot: error must be IN THE DOM, not just response body"* with the negative-test submit procedure — call `form.submit()` (real browser navigation, server's 422 + error HTML renders into the page) NOT `fetch(form.action, ...)`. Includes seven concrete `evaluate_script` / `wait_for` / `take_screenshot` steps and two named anti-patterns.

- **Executor agent contract update** (`agents/pr-test-executor.md`): new section *"Negative-test screenshot contract (v0.6.8+, mandatory for error_type items)"*. Mandates that for any item with `error_type` set, the screenshot must be taken AFTER the rendered error chip is in DOM (verified via `document.body.innerText` grep), `screenshots[].focus` must point at the chip's screen position, and evidence must explicitly say "rendered in PAGE DOM" (not "response body").

- **Mechanical enforcement** (`scripts/validate_screenshots_contract.py`): new identical-negative-screenshot byte-size lint. After the existing per-bucket count check, scan all negative-classified items pairwise (O(n²); typically n ≤ 5). If two negative items' primary screenshot is the same file size AND both are above 50 KB (heuristic floor to skip legitimate tiny stubs), emit a violation naming both item IDs and the byte size. New `check(plan, results, run_dir=...)` signature; `run_dir` is required for the byte-size lint (count-based contract unaffected when omitted, for backward compatibility).

- **Wired into `proctor_run.py`**: same `_ss_check` call at the EXECUTED→REPORTED boundary now passes `run_dir=run_dir` so the new lint runs in production. Pipeline aborts before report-render if the t-007/008/009 signature is detected.

**Tests** (`tests/test_helpers.py`) +5:

- `test_ss_check_identical_negative_screenshots_warns` — pins the literal v0.6.6 t-007/t-008 signature: two negative items pointing at the same 244252-byte stub, lint emits one violation containing both item IDs and the byte size.
- `test_ss_check_distinct_negative_screenshots_ok` — two distinct files of different sizes: zero violations.
- `test_ss_check_identical_below_floor_not_flagged` — same tiny file under the 50 KB floor: zero violations (legitimate sentinels exempt).
- `test_ss_check_identical_happy_save_screenshots_ok` — two happy-save items sharing a screenshot: zero violations (lint targets negative items only).
- `test_ss_check_identical_no_run_dir_skipped` — without run_dir, byte-size lint is silently skipped; count-based contract still runs. Backward compatibility preserved.

Tests 271 → 276.

### Why this is the right shape

Same v0.6.5 / v0.6.6 pattern: a real production bug ends with the user identifying it visually → ship the missing executor knowledge as skill + agent doc, regression-test the detection, mechanically enforce so prose alone isn't relied on. The fetch() vs form.submit() distinction is the surgical fix; the byte-size lint is the safety net that catches future regressions of the same shape.

## v0.6.7 — 2026-05-14

### Classifier: round-trip items no longer misclassified as edit-and-switch

The v0.6.6 e2e run against mcd-website PR #1115 exposed a regex ordering bug in `validate_screenshots_contract.py`. Item t-006b's `what` read:

> "HAPPY: re-open the just-edited reward — switched DigitalContentType, GameUrl, CTA labels all persist after hard reload"

The `_EDIT_AND_SWITCH_RE` (`\bedit\b.*\bswitch\b`) matched on "just-edited" + "switched" (past-tense verbs describing prior history, NOT the action under test in this item). Result: t-006b was bucketed as `edit-and-switch` (requires 3 screenshots) when it's actually a `round-trip` re-open verification (requires 2). The executor had to add a third screenshot purely to satisfy the false-positive classification.

**Fix**: re-order `classify_item` so `_ROUND_TRIP_RE` is checked first. Re-open / hard-reload phrasing is unambiguous — no save action happens inside such an item — so when it matches, the bucket is round-trip regardless of whether edit/switch verbs are present in past-tense context.

Pinned with `test_ss_classify_round_trip_after_edit_not_misclassified_as_edit_switch` (the literal t-006b plan-item text). Tests 270 → 271.

## v0.6.6 — 2026-05-14

### Teach the executor to satisfy upstream-validator preconditions

The v0.6.5 run against mcd-website PR #1115 (run-id `pr1115-e6a7c79-828594b8`) finished cleanly with 0 failures but skipped 9/11 items. Every save-flow item that needed to exercise the new `DigitalContent-Validator` was blocked by the basic `Image-Validator` (`Reward Image cannot be blank`) firing first — qor's MediaBox upload modal looked unreachable from headless chrome, so the executor took the empirical-grounding-rule's only remaining out: `precondition-not-met`. The new validator branches went unexercised, the test plan delivered 2/11 signal, and the user (correctly) flagged that the executor agent had no instructions for getting past an upstream-validator gate.

**Reconnaissance findings** (local server `http://localhost:9801`, qor admin + media MediaBox v0.0.0-20210903074215):

- The MediaBox renders a hidden `<textarea name="QorResource.<Field>" class="qor-field__mediabox-data">` that stores the selected file as `JSON.stringify([{ID, Url, ...}])`. The basic Image-Validator only checks the textarea's string for emptiness (`"" || "null" || "[]"` → reject; anything else → accept). No DB lookup, no S3 round-trip, no FK constraint.
- The qor MediaBox modal's backing data lives at a separate admin resource (`/admin/media_library?filters[SelectedType].Value=image` for Reward Image; `/admin/digital_download_assets` for Digital Download Asset). Existing records' primary keys are readable from the index page's `data-primary-key="(\d+)"` attribute via plain `fetch(...).text()`.
- Direct textarea injection with `[{"ID":5,"Url":"//x"}]` followed by `FormData(form)` + `fetch(form.action, {method:'POST'})` saves cleanly: HTTP 200 redirecting to `/admin/digital_content/<new-id>`. The asserted DigitalContent-Validator then runs as the next validator in the chain — all four branches reachable (Image-DDA-missing, Game-URL-empty, Game-URL-invalid, empty-DCT, plus Game-URL-valid → save succeeds).
- Round-trip survives: navigating back to `/admin/digital_content/<id>` shows the saved fields, so the v0.6.4 "round-trip" + "edit-and-switch" templates are exercisable too.

**New skill** (`plugins/proctor/skills/satisfying-form-preconditions/SKILL.md`):

- **Detection** — the trigger is a save-flow item whose first attempt returns an error message matching `cannot be blank` / `is required` / `must be present` on a field NOT named by the test's `how:`. This is empirically observable from the response body; not session memory.
- **Pattern A: existing-record reuse** (preferred — no upload required) — five concrete steps for the qor MediaBox case with code snippets for `take_snapshot` → `data-mediabox-url` extraction → `fetch` → `data-primary-key` grep → JSON injection → `FormData` + `fetch` submit. Generalizes to ActiveAdmin attached-blob and React-admin-with-hidden-input shapes.
- **Pattern B: real upload via the modal** — fallback when the picker isn't backed by a separate admin resource. Step-by-step `upload_file` + thumbnail poll + modal-dismiss flow with the `proctor-e2e-stub-<timestamp>.png` filename convention.
- **What NOT to do** — three documented anti-patterns the executor must avoid (skipping on first attempt, filling with placeholder strings, fabricating non-existent record IDs).

**Executor agent contract update** (`agents/pr-test-executor.md`):

- New section **2c. Upstream-validator precondition** (mandatory before any save-flow `precondition-not-met` skip). Lists the detection trigger, instructs the agent to read the new skill, mandates the evidence string call out which bypass technique was used.
- Tightened the precondition skip path: justification requires citing both Pattern A and Pattern B failure modes, not just one.

**Tests** (`tests/test_helpers.py`):

- New regression `test_satisfying_form_preconditions_detection` — pins the detector regex against the exact error strings observed in the v0.6.5 t-002 evidence (`Reward Image cannot be blank`, plus the generic `cannot be blank` / `is required` / `must be present` family). If a future executor rewrite drops the detection logic, this test catches it before the run hits production again.

### Why this is the right shape

v0.6.6 follows the v0.6.5 / v0.6.2 / v0.6.1 pattern: a real production skip that ended with the user saying "we should have been able to test this" → ship the missing executor knowledge as a skill, link it from the agent, regression-test the detection. The mechanical screenshot check from v0.6.5 stays — this fix doesn't loosen any contract, it adds a recovery path that wasn't there before.

## v0.6.5 — 2026-05-14

### Mechanical enforcement of the v0.6.4 screenshot contract

v0.6.4 introduced the per-item-type screenshot-count contract (render 1, negative 1, happy-save 2, round-trip 2, edit-and-switch 3) as prose discipline on the executor agent. e2e-driver run against PR-1115 confirmed prose alone is insufficient: pre-v0.6.4 production runs shipped t-006 ("edit reward, switch Digital Content Type from Image to Game") with one screenshot whose contents didn't even show the field being asserted on. The contract needs a structural backstop.

**New script** (`plugins/proctor/scripts/validate_screenshots_contract.py`):
- `classify_item(item)` — pure-function classifier that maps a TestPlan item to one of `{not-chrome-devtools, render-check, negative, happy-save, round-trip, edit-and-switch}`. Reads `tool`, `error_type`, `what:`, `how:`. Documented heuristic order so reviewers can read a plan and predict which items will be screenshot-enforced.
- `check(plan, results)` — returns a list of violation strings, one per item whose result has fewer screenshots than its bucket's minimum. Counts both the new `screenshots: [{path, label, focus}]` list (valid entries only) and the legacy `screenshot_ref` (as 1, for the render-check / negative floor). Skipped items exempt — they have no evidence to capture. CLI mode emits violations to stdout + exits non-zero.

**Pipeline wiring** (`plugins/proctor/scripts/proctor_run.py`):
- `_STEP_APPROVED` (executor finished, transitioning to fix/report decision) now runs `validate_screenshots_contract.check()` against the run's TestPlan + TestResults after schema validation passes.
- On violation: emit an `error` envelope with the full violation list. Pipeline aborts before report-render; the developer sees the gap before the run is "complete" rather than discovering useless screenshots in the published report.

**Agent prose update** (`plugins/proctor/agents/pr-test-executor.md`):
- New paragraph under the v0.6.4 "Screenshots are PROOF" section calling out the v0.6.5 mechanical check and what aborting looks like. The agent still describes the contract in detail; the mechanical check is the floor, not the ceiling.

### Tests
- 243 → 268 (+25): classifier round-trips on each bucket; check returns empty on satisfied minimums; check flags every minimum-violation case; legacy `screenshot_ref` counts toward render-check floor but not happy-save (preserves backward-compat without raising the ceiling); non-chrome-devtools items exempt; skipped items exempt; plan/results-drift items silently skipped; pinned regression case mirroring the actual pre-v0.6.4 t-002/t-003/t-006 result shape against a representative PR-#1115 plan — all three flagged.

### Why this is structural

v0.6.4 belongs to the family of "make the LLM behave better via better prose". v0.6.5 belongs to the family of "make the LLM's mistakes loudly visible at validation time so they cannot ship unnoticed". The same delineation as v0.6.1 (pipeline state machine over prose loop discipline) and v0.6.2 (`validate_item_result.py` over executor agent prose forbidding preemptive skip). The pattern: when prose enforcement of a rule produces a real production failure, ship a mechanical check that fires before the artifact is finalized.

## v0.6.4 — 2026-05-14

### Screenshots as proof — per-item-type contract for evidence

User's v0.6.3 run report had t-006 ("edit reward, switch Digital Content Type from Image to Game") with a single post-save screenshot that DIDN'T EVEN SHOW the Digital Content Type field. Useless as evidence — the screenshot was a different page than what the test was asserting on. v0.3.40 had a generic "set screenshot_focus so the screenshot corroborates the evidence" rule; in production the executor took ONE screenshot at the assertion point regardless of whether one frame could carry the proof.

**Schema** (`scripts/schema.py`):
- New optional `screenshots: [{path, label, focus}]` field on test-result items.
- Coexists with legacy `screenshot_ref` + `screenshot_focus` (reporter prefers the list; falls back to single-shot for v0.6.3-and-earlier results).
- Each entry's three fields are required + non-empty.

**Executor agent contract** (`agents/pr-test-executor.md`):
- New "Screenshots are PROOF, not decoration" section.
- Per-item-type screenshot count + content matrix:
  - **Render-check**: 1 — new field(s) with labels visibly in frame.
  - **Negative**: 1 — inline error message AND the field together.
  - **Happy save**: 2 — (a) form filled, (b) post-save success state.
  - **Round-trip**: 2 — (a) detail page initial, (b) post-hard-reload same fields.
  - **Edit-and-switch**: 3 — original / changed / persisted.
  - **Multi-step flow**: 1 per logical step.
- Pre-screenshot requirements:
  - Must `take_snapshot` first to confirm field is on the page.
  - Must scroll the asserted field into view via `evaluate_script('document.querySelector(...).scrollIntoView({block: "center"})')` BEFORE take_screenshot.
  - Format: PNG, **viewport-cropped** (`fullPage: false`). Full-page screenshots make assertions 30px tall in a 4000px image — unreadable.
  - Filename: `<id>__<n>__<short-label>.png` — self-documenting.
- 5 explicit anti-patterns called out (real ones we've seen), including the t-006 "post-save detail page when assertion is on form-state change" failure.

**Renderer** (`scripts/render_item_artifacts.py`):
- New `screenshots` parameter (list of `{path, label, focus}` dicts) takes precedence over legacy single-screenshot fields.
- Renders each as numbered list entry: `1. **<label>**` + image embed + `_Focus:_ <focus>`.
- Per-entry existence check — one missing screenshot doesn't break the whole block; that one gets "(file not found)" while siblings render normally.
- CLI gets `--screenshots-json` arg accepting a JSON-encoded list.

### Tests
- 235 → 243 (+8): `screenshots` list accepted; missing required key rejected; empty-string field rejected; non-list rejected; multi-screenshot block rendered (3 entries, 3 focuses); per-entry missing-file fallback; new field takes precedence over legacy; legacy single-screenshot still works.

## v0.6.3 — 2026-05-14

### Fix v0.6.2 validator false-negative on the exact bug it was shipped for

Acceptance subagent ran v0.6.2's `validate_item_result.py` against the user's literal v0.6.1 t-005 evidence (pinned verbatim) and the validator emitted nothing — false-negativing the production failure mode.

**Root cause**: `_OBSERVED_MARKERS` included this pattern:
```
r"\battempt(?:ed|s)?\b.*\b(?:fail|error|reject|stuck|hung)"
```
The intent was to match "attempted save; failed with X". But the regex is unanchored + uses `.*`, so it falsely matches descriptive future-tense narration like "the CMS attempts to call mcd-services' CreateReward RPC. Creating ... and fails BEFORE backend handling". Code-inspection prose routinely says exactly that — the regex matched the very pattern the validator was built to catch.

**Fix**: removed the `attempt...fail` marker entirely. The remaining markers (exit code / HTTP NNN / stderr / stdout / server returned / curl returned / DOM snapshot / navigated to / connect: connection refused / explicit no-attempt disclaimer) cover legitimate empirical captures without a `attempted` alias.

**Regression tests** (added in this release):
- `test_vir_v061_t005_actual_evidence_flagged` — pins the LITERAL t-005 evidence string from the user's v0.6.1 run. Must flag. Test docstring explains "don't simplify this string — it's the production failure mode pinned verbatim".
- `test_vir_v061_t007_actual_evidence_flagged` — same for t-007.
- `test_vir_v061_t009_actual_evidence_flagged` — same for t-009.

These three tests function as a permanent guard: if a future change adds a regex that re-loosens the validator to false-negative on this exact prose pattern, CI fails.

### Tests
- 232 → 235 (+3 regression fixtures).

## v0.6.2 — 2026-05-14

### Forbid preemptive skipping — empirical-grounding validator for executor results

User: "本地怎么还会skip呢？我已经换成正确的env了，怎么还会skip" — after fixing the env, the v0.6.1 run still skipped 3 happy-save items with `reason=precondition-not-met`. Inspection of the skip evidence:

```
t-005 evidence: "local dev_env's empty/dev PASETO key in pkg/auth
blocks the gRPC client's chacha20poly1305 token construction..."
```

The executor (main AI executing inline rather than via per-item subagent dispatch) **never tried** — it carried session memory of a prior chacha20poly1305 error from earlier in the conversation and skipped preemptively. The user's env had been fixed; those items could have passed. The skip evidence was pure code-inspection reasoning, no captured stderr, no observed failure.

This release ships mechanical enforcement.

**New helper** (`plugins/proctor/scripts/validate_item_result.py`):
- Reads a single-item result OR a full TestResults JSON.
- For items with `status=skipped` and `reason ∈ {precondition-not-met, environment, data-template-missing}`:
  - Checks evidence for empirical-grounding markers: `exit code: N`, `HTTP <num>`, `stderr:`, `stdout:`, `DOM snapshot shows`, `server returned`, `curl returned`, `navigated to ...`, `connect: connection refused`, explicit "did not attempt because..." disclaimer, etc.
  - OR checks for a non-empty `command:` field.
  - If neither: emits a warning `<id>: status=skipped reason=... but evidence appears to be code-inspection reasoning ...`.
- Propagated skips (`reason=data-dep-failed: <id>`) are exempt — empirical grounding lives on the upstream item.
- Doesn't override status. Validation is advisory; the warning surfaces to the reporter so the human reviewer sees the gap.

**Executor agent contract** (`agents/pr-test-executor.md`):
- New "NO preemptive skip (v0.6.2+, mandatory)" section at the top of the procedure.
- Explicit forbidden: code inspection, session memory, general reasoning.
- Required: attempt first action → capture response → cite captured artifact in evidence.
- Names the v0.6.1 failure mode in prose so future readers see what the rule is fighting.
- "If you find yourself about to write a `precondition-not-met` skip with no captured stderr/HTTP/DOM snapshot — STOP. Go run the first attempt. Then come back with the real observation."

**Executor SKILL** (`executing-pr-tests/SKILL.md` Step 4):
- New "Empirical-grounding check (v0.6.2+)" — after each subagent result, pipe through `validate_item_result.py`. Append warnings to the run's evidence chain so the report renders them.
- Notes that per-item subagent dispatch (the proper executor path) eliminates session-memory leaks structurally — fresh context per item. The validator catches it even when dispatch is bypassed (e.g. main AI hand-executing).

### Why this is structural, not prose

Every prior version that asked the executor "be careful" in prose has been ignored by an AI under pressure. The validator is a `Bash` tool call with deterministic output — it can't be skipped. Warnings flow into the evidence chain mechanically. The reporter renders them. The reviewer sees them. No prose adherence required for the visibility part.

The classification itself still depends on the executor honoring the contract. The validator catches DISHONEST classifications (no empirical evidence) but can't force the executor to actually attempt. Combined with per-item subagent dispatch (which isolates context and removes the session-memory leak), the two layers together are the best defense available without platform-level enforcement.

### Tests
- 222 → 232 (+10): pass-item no warning; propagated skips (data-dep-failed / data-template-missing) no warning; precondition skips with HTTP / exit code / stderr / DOM evidence no warning; with `command:` field no warning; code-inspection-only skip warns (the EXACT v0.6.1 t-005 + t-009 evidence strings used as fixtures); explicit "did not attempt" disclaimer no warning; unknown skip reasons not checked; check_results walks items; CLI reads single-item from stdin.

## v0.6.1 — 2026-05-14

### `/proctor-drive` — bypass the AI turn-model stall via subagent dispatch

v0.6.0 shipped the pipeline state machine. Acceptance-test subagent ran it end-to-end (9 iterations, 5 stages, clean `done` envelope). But the user's main Claude Code session **still** stalled at the same point: after `dispatch_skill` returns and the Skill writes its artifact, the AI ends its turn instead of re-invoking `proctor_run.py`. Trace: `Brewed for 2m 51s` after Stage 1 emitted its "done" status, no continuation, user had to type "继续".

**Diagnosis** (from the successful subagent run): the state machine architecture is correct. The stall is a Claude Code platform-level constraint — the main session's AI can end a turn after any tool call. Prose can't structurally prevent that. The subagent didn't suffer because subagent tasks run end-to-end as one unit; there's no inter-step prompt where the AI gets to stop.

**The structural answer**: dispatch the pipeline as a subagent. New `/proctor-drive` command does exactly that.

### `commands/proctor-drive.md` (new)

- Captures `$ARGUMENTS` (PR number/URL).
- Dispatches ONE `general-purpose` Agent with a verbatim copy of the v0.6.0 harness instructions.
- Subagent runs `proctor_run.py` in a loop, handling each envelope (`bash`/`dispatch_skill`/`show`/`ask_user`/`done`/`error`) without inter-stage stalls.
- When subagent returns, main session emits the report.html path and exits.
- Approval gate's `ask_user` is real — subagent calls `AskUserQuestion` like the main AI would.
- CI mode falls back to `/proctor:proctor` flow (CI doesn't go through interactive Claude Code session — the workflow runs the action directly, no turn-model stalls apply there).

### `commands/proctor.md` (header note)

- Top-of-file directive added: if you're reading this because the AI already stalled mid-pipeline, kill the session and use `/proctor-drive <PR>` instead.
- Recommends `/proctor-drive` for fresh starts too.
- `/proctor:proctor` still works for users who want the multi-iteration loop in the main session; it just isn't reliable for fully-unattended runs.

### Why this is acceptable as the "final" fix

The honest truth: prose-driven loop discipline in the main AI has been the failure mode across every release v0.3.32 → v0.6.0. Six different "tightening" attempts all failed under pressure. The subagent acceptance test conclusively proved the architecture works when loop discipline is enforced. The platform constraint (AI ends turns after tool calls) cannot be worked around via prose. Wrapping the pipeline in a subagent is the structural fix.

The user's actual operational concern was CI deployability — that's solved because CI doesn't use the interactive session. Local users get `/proctor-drive` as the reliable entry point. `/proctor:proctor` stays as the lower-level command for users who want to drive the state machine themselves.

### Tests
- 222 unchanged (no new helpers; the change is the command layer + harness prose).

## v0.6.0 — 2026-05-14

### Pipeline orchestrator → Python state machine (matches v0.5.0 wizard refactor)

v0.5.0 moved the wizard's control flow into a state machine and eliminated the inter-step stalls there. v0.6.0 extends the same architecture to the main `/proctor:proctor` pipeline. The 9 stages of prose with explicit "after stage X → do Y" directives are replaced with a state machine that drives stage transitions; the AI's role compresses to "run the script, dispatch the indicated action, re-invoke".

**New driver** (`plugins/proctor/scripts/proctor_run.py`):
- Reads `.proctor/runs/<run-id>/pipeline-state.json`, advances one transition per invocation.
- 6 envelope types: `bash` (run command), `dispatch_skill` (invoke Skill tool), `show` (emit markdown), `ask_user` (AskUserQuestion), `done` (terminal), `error`.
- 10 state transitions cover the happy path: INIT → FETCHED → ANALYZED → PLAN_DISPATCHED → PLANNED → TABLE_SHOWN → APPROVED → EXECUTED → (FIX_DECIDED → FIXED) → REPORTED → DONE.
- Validates artifact JSON via `schema.py` at each stage boundary; surfaces schema errors as `error` envelopes before dispatching the next stage.
- Conditional fix dispatch: only invokes `proctor:fixing-test-failures` when `summary.fail > 0`. No failures → writes `fix-pr-ref.json = null` and skips Stage 4.
- Aborted runs (e.g. `force-push`) skip fix + jump straight to report so the user sees what happened.

**New orchestrator harness** (`plugins/proctor/commands/proctor.md` top section):
- Replaces the 9-stage prose with a tight `while`-style loop description: invoke `proctor_run.py`, parse envelope, dispatch one action, re-invoke.
- Loop discipline explicitly forbids inter-stage stalls — only `done`/`error`/awaiting-AskUserQuestion legitimately end the turn.
- Mode detection + state-file path setup happens ONCE in pre-flight; state machine handles everything after.
- Legacy 9-stage prose stays below as fallback documentation + reference for any flow the state machine doesn't yet handle (CI mode's `require_approval=true` early-exit + mutex acquire).

### Why this matters operationally

User's session-long complaint was "如果部署 ci 的话根本没办法执行" — the inter-stage stalls in `/proctor:proctor` made the pipeline impossible to run non-interactively (CI deployment, sandbox automation, etc.). v0.6.0 removes those stalls by removing the AI's discretion at each stage boundary:

| Stage boundary | v0.4.x failure mode (observed) | v0.6.0 behavior |
|---|---|---|
| Stage 1 → 2 | AI dumped ChangeMap JSON, churned 3m+ | State machine emits `dispatch_skill` for plan; AI dispatches and re-invokes |
| Stage 2 → approval gate | AI dumped TestPlan JSON, churned 5m+ | State machine emits `bash` for render_plan_table.py, then `ask_user` |
| Approval gate → Stage 3 | AI churned waiting for "what's next" | State machine immediately emits `dispatch_skill` for execute |
| Stage 3 → Stage 4 | AI hesitated on "fix or report?" | State machine reads test-results.summary.fail, emits the right skill |
| Stage 4 → 5 | Same | State machine routes to report |

Plus all the v0.4.x partial fixes (render_plan_table.py from v0.4.3, render_item_artifacts.py from v0.4.6, etc.) remain in place — the state machine USES them via `bash` envelopes rather than the AI hand-running them mid-stall.

### Tests
- 209 → 222 (+13): first invocation requires pr-arg; first invocation emits bash for fetch; post-fetch dispatches analyze; post-analyze validates + dispatches plan; post-plan emits bash for render; post-render emits ask_user approval; "Run all" dispatches execute + writes approved-plan.json; "Cancel" emits done; no-failures result skips fix + writes null; failures result dispatches fix; aborted result skips fix to report; report stage emits done with report.html URL; corrupted state resets gracefully.

### Still v0.6.x territory

- CI mode `require_approval=true` early-exit (post plan as PR comment + exit 0 for re-run on `/proctor run` comment trigger).
- Mutex acquire/release (concurrent CI run coordination).
- "Drop specific items" approval-gate path (currently only "Run all" and "Cancel" are implemented).

These will be additive state transitions in v0.6.x; the v0.6.0 happy path works end-to-end for local runs and CI runs that don't need approval gating.

## v0.5.0 — 2026-05-14

### Wizard control flow → Python state machine

User feedback after a session of inter-step stalls (`Crunched for 1m 14s` / `Brewed for 21s` / `Cooked for 1m 18s`, requiring "继续" prompts to unstick): "如果部署 ci 的话根本没办法执行". The wizard's prose-driven multi-step procedure gave the AI too many "what's next" decision points; each transition was a stall opportunity.

The structural fix (deferred since v0.4.5's CHANGELOG noted it as v0.5 territory): move the wizard's control flow out of prose into a Python state machine. v0.4.3 / v0.4.5 / v0.4.6 already validated this pattern for specific surfaces (approval-gate render / MODE decision / artifact rendering). v0.5.0 extends it to the wizard's overall control flow.

**New driver** (`plugins/proctor/scripts/wizard_run.py`):
- State-machine driver that reads `.proctor/wizard-state.json`, advances by ONE state transition per invocation, writes state back, and emits exactly one JSON envelope describing what the AI should do next.
- 5 envelope types:
  - `ask_user` — AI calls AskUserQuestion with the spec, then re-invokes with `--answer "<label>"`.
  - `show` — AI emits the markdown to chat, then re-invokes.
  - `bash` — AI runs the command, then re-invokes with `--bash-rc <exit_code>`.
  - `done` — AI emits summary, exits the loop.
  - `error` — AI emits the error, exits.
- The state file is the only thing carrying context between invocations. Safe to interrupt mid-flow; re-invoking resumes.

**New atomic bump action** (`plugins/proctor/scripts/wizard_bump_action.sh`):
- Single shell script: sed-replace pin → git diff → git add → git commit → git push. One Bash tool call from the wizard's `bash` envelope. Eliminates the v0.4.x bump-only stall path (edit + diff + commit + push → 4 separate tool calls × ~1-3min churn each).
- Portable (BSD sed on macOS, GNU sed on Linux). Optional `--no-push` flag.
- Exit code 4 if push fails; commit is still in place locally.

**New harness** (`plugins/proctor/commands/proctor-init.md` top section):
- Replaced the open-ended "lead the user through the steps" prose with an explicit `while`-style loop description: invoke `wizard_run.py`, parse envelope, dispatch one action, re-invoke. Loop discipline explicitly forbids ending the turn between iterations.
- Stop conditions enumerated: only `done` / `error` envelopes / displayed-and-awaiting-answer AskUserQuestion are legitimate end-of-turn states. Anything else is a stall.
- The legacy 1300-line prose stays below the harness as documentation + fallback for modes not yet migrated to the state machine.

### What the state machine implements (v0.5.0 scope)

- `current` — fully configured, exit with summary.
- `bump-only` — emit `bash` envelope invoking `wizard_bump_action.sh`. One AI iteration: invoke script, get exit code, re-invoke wizard, get `done`. Two iterations total instead of v0.4.x's 4+ stalled steps.
- `needs-local-regen` — emit `ask_user` for the 3-option question, then branch on answer (Regenerate / Just run / Skip) to either a `show` envelope pointing at legacy prose for the regen flow OR `done`.
- `legacy-migration` — emit `ask_user` for the migration question, then branch to either a `show` envelope pointing at the v0.4.0 git-mv block OR `done`.

### What's still in legacy prose (v0.5.x will migrate)

- `fresh` — full new install (1300 lines of stack detection + auth setup + file generation). The state machine emits a `show` envelope routing to Sections 1-8 of the legacy prose.
- `migrate` — v0.2 → v0.3 migration (adds auth block, drops `setup:`). Legacy prose Section 7-8.
- `bump-only-with-seed` — pin bump + Step 8c-pre seed script regen. Legacy prose Section 8 + 8c-pre.

For these modes the AI walks legacy prose manually. The state machine returns `done` after emitting the `show` envelope so the wizard's outer loop terminates cleanly; the AI then handles the prose section in subsequent turns.

### Why this is the structural fix

Every prior v0.3.x / v0.4.x release that tightened wizard prose tried to fix the same symptom (AI stalls between steps) with the same approach (loud "MUST" directives). All failed in production runs to some degree. v0.4.3 / v0.4.5 / v0.4.6 found a different shape that works: take the deterministic logic out of prose, put it in a script the AI dispatches. v0.5.0 extends this from "specific surfaces" to "the wizard's control flow itself".

The AI's job is now I/O relay across 3-4 iterations max for the fast modes. Each iteration's prose responsibility is bounded: parse one envelope, dispatch one action. No multi-step procedure to interpret, no "what next" decision points between iterations.

### Tests
- 198 → 209 (+11): first invocation on user-bug scenario emits ask_user (the exact PR-1115 / mcd-website case); current mode emits done immediately; bump-only emits bash with script invocation; after bash success → done; after bash failure → done with warning; needs-local-regen for each of the 3 options; fresh mode falls back to legacy prose; state file persists between invocations; corrupted state file resets gracefully.

### Next: v0.6

Same pattern for `/proctor:proctor` orchestrator. The main pipeline (analyze / plan / execute / fix / report) has the same inter-stage stall problem. v0.6.0 will be a `scripts/proctor_run.py` state machine that dispatches each stage's skill and the AskUserQuestion approval gate; AI's role compresses to "run script, relay envelopes". Shipping after v0.5.0 sees a real run.

## v0.4.6 — 2026-05-14

### Reporter artifact links — absolute file:// URLs + loud "missing artifact" badges

User opened `file:///path/to/.proctor/runs/<id>/report.html` and "Full log" links 404'd with `ERR_FILE_NOT_FOUND`. Also: 11 chrome-devtools items, 0 screenshots — yet report rendered nothing acknowledging the gap.

**Two bugs, both same root cause** (AI hand-rendered something prose said should be computed deterministically):

1. **Log links 404'd** because reporter rendered repo-root-relative hrefs `.proctor/runs/<id>/t-001.log`. The browser resolves those relative to the REPORT's directory (`.proctor/runs/<id>/`), giving `.proctor/runs/<id>/.proctor/runs/<id>/t-001.log` — double-nested, doesn't exist. The reporter SKILL prose said "absolute path to item.logs_ref" but the AI rendered repo-relative anyway.
2. **Missing screenshots silently absent.** The pr-test-executor agent contract REQUIRES `screenshot_ref` for chrome-devtools items, but real runs show subagents skipping `take_screenshot`. Reporter's `{if item.screenshot_ref}` skipped rendering, hiding the gap. Reviewer couldn't tell "test passed without screenshot" apart from "test passed and there's nothing visual to show".

**Same fix pattern as v0.4.3 / v0.4.5**: move the deterministic logic out of AI prose into a script.

**New helper** (`plugins/proctor/scripts/render_item_artifacts.py`):
- Reads run-dir + item fields, normalizes the artifact ref against (a) absolute path, (b) `<run-dir>/<sub-dir>/<basename>` (the executor's canonical write location), (c) `<cwd>/<ref>` (repo-root-relative — what AIs default to). Returns the first one that EXISTS.
- For LOGS: emits `**Full log:** [<name>](file:///abs/path)` in local mode, or `... (in artifact)` link in CI mode. If the file is missing despite a ref → italic "(not found at <path>)" badge.
- For SCREENSHOTS on chrome-devtools items:
  - present → image embed + `_What to look for:_` line from `screenshot_focus`.
  - **ref present but file missing** → "(file not found at <path>)" badge.
  - **ref absent entirely** → loud `**Screenshot:** *(not captured — chrome-devtools items REQUIRE a screenshot per the pr-test-executor agent contract, but screenshot_ref is absent on this result. Treat as an executor bug; the test may have passed without visual verification.)*` — surfaces the contract violation instead of silently rendering nothing.
- Lint-only items with no logs / no screenshots → returns empty string (no spurious sections).

**Reporter SKILL** (`reporting-pr-test-results/SKILL.md`):
- Per-item artifact subsection replaced with a single bash call to `render_item_artifacts.py`. AI passes the item's fields; script's stdout goes into the report verbatim.
- "Do NOT hand-render `file://` or `**Full log:**` lines yourself — that's what produced the v0.3.x 404 bug" added to the forbidden list.

**Executor SKILL** (`executing-pr-tests/SKILL.md`):
- New post-dispatch artifact-capture check: after receiving each subagent result, log a warning when `logs_ref` is empty (any item) or `screenshot_ref` is empty (chrome-devtools items). Status NOT downgraded — the test may have passed fine; the gap is just visibility. The reporter's script will surface the missing artifact visibly.

### Tests
- 190 → 198 (+8): local log absolute file:// URL when ref is repo-root-relative; missing log "(not found)" badge; chrome-devtools missing-screenshot loud warning (verifies the EXACT bug user hit); chrome-devtools present-screenshot image embed + focus line; chrome-devtools repo-root-relative screenshot ref resolves to actual path; lint-only with nothing to render returns empty; CI mode emits artifact URL instead of file://; CLI runs end-to-end.

## v0.4.5 — 2026-05-14

### Wizard MODE decision moved into a deterministic script — fixes silent NEEDS_LOCAL_REGEN skip

v0.4.4 added a NEEDS_LOCAL_REGEN branch to the wizard so it would AskUserQuestion when `.proctor/local.yml` was missing. The user's real run on v0.4.4 silently skipped that branch and went bump-only anyway. Subagent diagnosis: the AI walked the MODE-detection prose bullets, found a later "bump-only by pin age" bullet keyed on directly-observable file facts (`grep workflow for current pin`), matched it, and never re-evaluated whether the earlier NEEDS_LOCAL_REGEN bullet — keyed on a detection-block-computed variable — should have fired first.

This is the same class of failure mode as v0.4.3 fixed for the approval-gate render: prose-driven AI control flow with multiple plausible branches doesn't reliably pick the right branch. The structural fix is the same: move the decision out of prose into a deterministic script.

**New helper** (`plugins/proctor/scripts/wizard_decide_mode.py`):
- Reads the consumer repo's actual file state (config.yml, local.yml, seed script, workflow pin, auth block).
- Walks priority-ordered rules and prints a single JSON object: `{state, mode, next_action, ask_user}`.
- `mode` is one of: `fresh`, `legacy-migration`, `needs-local-regen`, `bump-only-with-seed`, `migrate`, `bump-only`, `current`.
- `ask_user` is `null` (no input needed) or a `{header, question, options[]}` AskUserQuestion spec.
- The priority ordering encodes which scenarios "win" when multiple are technically true: legacy-migration first (must migrate before evaluating anything else), then needs-local-regen, then seed-script regeneration, then v0.2→v0.3 migrate, then pin bump, then current. The NEEDS_LOCAL_REGEN case can no longer be swallowed by a later bump-only rule because the script returns a single answer.

**Wizard** (`commands/proctor-init.md` Section 0.5):
- New "v0.4.5+ deterministic decision (REQUIRED FIRST STEP)" block at the top of MODE detection.
- AI's procedure: (1) run the script, (2) read the `mode` field — THAT is the branch, (3) if `ask_user` is non-null, immediately AskUserQuestion with that spec.
- The existing prose bullets are demoted from "decision tree" to "MODE reference look-up table". Section header changed from "Branch on what's there" to "MODE reference (script picked one of these — look up the action; do NOT re-evaluate against these conditions)".
- MODE summary table gained rows for `legacy-migration`, `needs-local-regen`, `bump-only-with-seed`, `current` (previously only `fresh`/`migrate`/`bump-only` were covered).

### Why this is the right fix (and what's still TODO)

The script removes the AI's discretion at the decision point. Once the right `mode` is in JSON, the AI's remaining job is mechanical: dispatch the AskUserQuestion. This pattern — moving deterministic decisions out of AI prose — is the same one v0.4.3 used for the approval-gate render. The lesson: every place the AI has prose-driven discretion between "I observed state X" and "I should take action Y", a script is a more reliable bridge than a bulleted list.

Still TODO (separate ship): the wizard ALSO stalls between sequential steps (the user typed "继续" three times during the v0.4.4 run, after commit, after push, after seed). The decision script doesn't address that — it's a different pattern (sequential-procedure stall vs. branching-decision stall). v0.5 territory.

### Tests
- 179 → 190 (+11): scenario coverage for every rule branch — fresh install, legacy layout, needs-local-regen on the exact user scenario (the test case is literally "the bug the user hit on v0.4.4"), bump-only when local.yml present + pin old, current when pin matches + local present, bump-only-with-seed when seed script missing, migrate when no auth block, current-tag missing doesn't force bump-only, pin extraction, pin-none when workflow missing, CLI emits valid JSON.

### How to use

```bash
claude plugins remove proctor
claude plugins marketplace remove zealllot-proctor
claude plugins marketplace add zealllot/proctor
claude plugins install proctor@zealllot-proctor

cd <repo>
claude
/proctor:proctor-init
# Wizard runs the decision script first thing.
# For the missing-local-yml scenario: AskUserQuestion fires with 3
# options. Pick "Regenerate seed-local.sh AND re-run it (Recommended)"
# to walk Step 7f setup-confirmation (catches wrong env-source which
# is what likely produced the chacha20poly1305 errors earlier).
```

## v0.4.4 — 2026-05-14

### Wizard detects missing `.proctor/local.yml` (don't silently fall through to bump-only)

User feedback: "因为 local 有问题，所以我删掉了，但是 init 没有检查重做" — they deleted `.proctor/local.yml` expecting the wizard to detect the gap and regenerate, but the wizard ran in bump-only mode and just bumped the action pin without checking if local.yml existed.

This release adds a new pre-flight detection + AskUserQuestion.

**New detection** (`commands/proctor-init.md`):
- `HAS_LOCAL_YML` — whether `.proctor/local.yml` exists
- `NEEDS_LOCAL_REGEN` — fires when seed script exists but local.yml is missing

**New MODE branch** when `NEEDS_LOCAL_REGEN=yes`: AskUserQuestion with three options:
1. **Regenerate seed-local.sh AND re-run it** (Recommended) — falls into the full Section 7 path so Step 7f setup-command confirmation runs (picks up v0.4.x setup-confirmation improvements + lets user fix the env-source mismatch that probably caused the local.yml problem in the first place). Step 8c-pre regenerates the seed script. Summary tells the user to `./.proctor/seed-local.sh`.
2. **Just run the existing seed-local.sh** — faster but uses whatever setup commands were baked into the seed script at wizard-time. Wizard's exit summary explicitly lists `./.proctor/seed-local.sh` as next step.
3. **Skip** — wizard does nothing extra, bump-only continues.

**Why "Regenerate seed-local.sh" is the recommended option**: an existing seed script is wizard-generated and bakes its `setup:` commands at generation time. If those baked commands are stale (e.g. wrong env-source file → server-config mismatch → chacha20poly1305 gRPC handshake failure that the user just hit), running the existing script as-is reproduces the same problem. Re-running Step 7f lets the user confirm the right env-source file + setup commands, and Step 8c-pre rebuilds the seed script with the fresh choices.

**Also fixed**: the "fully set up" branch now requires `HAS_LOCAL_YML=yes` too. Previously it would say "PRoctor is already integrated and up to date" even when local.yml was missing.

### Tests
- 179 unchanged (wizard prose only).

## v0.4.3 — 2026-05-14

### Approval-gate render moved out of AI prose into a deterministic script

Six releases of prose-tightening on the Stage 2 → approval-gate transition (v0.3.x: 4-substep gate, then 5-substep gate w/ hard-gate lint, then back to 4-substep, then v0.4.2's "no JSON dump + no thinking pause" warnings) all failed in production: the AI kept dumping the test-plan JSON to chat before rendering the table, consuming its own context budget and stalling for 3-5 minutes (`Churned for 4m 32s` on the v0.4.2 trace).

The pattern is structural: any time the AI has discretion between "I just generated/validated something" and "I need to take the next step", the show-work compulsion fires and burns context that the next-step intent needed. Prose can ask the AI nicely; it doesn't reliably comply.

This release removes the AI's discretion entirely for the approval-gate render.

**New script** (`plugins/proctor/scripts/render_plan_table.py`):
- Reads test-plan.json from stdin.
- Emits the markdown approval-gate block to stdout: header `## Plan for PR #<num> — <total> items`, the items table (one row per item, `id / Cat / Risk / Tool / As / What`), an `**Estimated:**` line summing per-tool runtime (`lint-only ≈ 5s`, `bash ≈ 30s`, `chrome-devtools ≈ 60s`) + dollar cost.
- Optionally surfaces `plan-smells.txt` residual warnings (when the planning skill exhausted its 2 regen attempts at self-audit) as a `### Plan smells (still present after 2 regen attempts)` section below the estimate, each warning a `⚠` bullet. Omitted when the file is absent or empty.
- Truncates over-long `what:` fields at 100 chars with `…` and collapses multi-line whitespace into single lines for clean table rendering.

**Orchestrator** (`commands/proctor.md` step 6):
- Approval gate is now TWO tool calls instead of four substeps:
  - **6a**: Bash invocation of `render_plan_table.py` — stdout goes to chat verbatim.
  - **6b**: AskUserQuestion with three options (Run all / Drop items / Cancel).
- Explicit "Do NOT hand-render the table" added to the forbidden-list — historical context from why this design was needed.
- Same response, no AI text between 6a and 6b.

### Why this is the right fix

The AI was never the right component for rendering deterministic markdown — table formatting is a pure function of the JSON. Doing it in a script means:
- Zero "show work" temptation: AI doesn't see the table being built, so it can't dump pre-render artifacts.
- Zero context cost: AI's working memory only holds "run script then ask question", not 13 rows of plan items.
- Deterministic output: every run renders identically for the same plan. Tests can pin format.
- Faster: bash script is milliseconds; AI rendering took 3-5 minutes including stalls.

The lesson generalizes: prose-tightening hits diminishing returns once the AI's prior compulsions overwhelm the rule. Moving the work to a script is the structural answer.

### Tests
- 170 → 179 (+9): header + row count, estimate format under/over a minute, long-`what` truncation, smells residual rendering when file present / absent / empty, multi-line `what` whitespace collapse, CLI stdin reading.

## v0.4.2 — 2026-05-14

### Re-tighten "no JSON dump + no thinking pause" on Stage 1→2 and Stage 2→approval transitions

User trace on v0.4.1 against PR #1115: AI completed Stage 1 (ChangeMap written + validated correctly), emitted the `[proctor:analyze] done` status line as instructed, THEN dumped the full ChangeMap JSON to chat anyway, THEN `Cogitated for 3m 29s` without invoking `Skill(planning-pr-tests)`. User had to type "continue" to unstick. Same failure mode seen earlier on Stage 2 → approval gate transitions.

Diagnosis: the rule "DO NOT print the JSON" existed but wasn't loud enough. The AI's compulsion to "show its work" overrode the prose ban. Once the JSON was in chat, the model's context budget couldn't hold the next-step intent and it stalled.

`commands/proctor.md` Stage 1 and Stage 2 transitions get explicit, surgical re-tightening:

- **Stage 1 (analyze)**: spelled out what specifically NOT to emit (full object / pretty excerpt / hunks-array-with-summaries / `cat change-map.json`). Added an explicit "your next assistant turn must contain EITHER (a) one-line status + Skill(planning-pr-tests) dispatch OR (b) an abort — NOT a JSON code block, NOT a thinking pause". Cites the exact failure mode (the "3+ minutes Cogitated" the user just hit) so future versions of the AI see the consequence of the rule, not just the rule.

- **Stage 2 (plan)**: same tightening. Emit the one-line status AND the 4 approval-gate substeps in the same response — no pause between status line and 6a header. The planning skill already self-audited; no "verify what was generated" preamble.

### Tests
- 170 unchanged (orchestrator prose only).

## v0.4.1 — 2026-05-14

### Tighten the v0.4.0 layout migration in /proctor:proctor-init

v0.4.0 shipped the migration block but the bash was fragile in three ways the user noticed when reviewing:

1. **No `[ -f ]` guard on `git mv .pr-test.yml`** — re-running the wizard after a partial migration would error ("`fatal: bad source, source=.pr-test.yml`"). Now every move is guarded; re-runs are idempotent.
2. **`sed -i.bak .gitignore` crashes when `.gitignore` doesn't exist** — exotic case but real. Now we `touch .gitignore` first.
3. **`printf >> .gitignore` always appends** — re-running the migration would duplicate the PRoctor lines. Now each line is `grep -qxF`-guarded before appending; the migration is fully idempotent.

Plus two user-facing improvements:

- **Preview step**: BEFORE moving anything, the wizard echoes the planned `git mv` operations + the `.gitignore` patch. User sees exactly what's about to happen.
- **Summary step**: AFTER moving, `git status --short` of the affected paths so the user can review the renames before staging / committing.

The `.gitignore` cleanup also drops legacy comment markers (`# PRoctor (...) — DO NOT COMMIT`-style headers) and the `hack/proctor-seed-local.sh` line if the user ever gitignored it (we don't, but defensive).

### Tests
- 170 unchanged (wizard prose only).

## v0.4.0 — 2026-05-14

### Consolidated consumer-side layout — single `.proctor/` directory

User feedback: "项目里的配置文件脚本什么的都放得好分散, 我都看不清有哪些文件" — PRoctor's files were scattered across the consumer's repo root (`.pr-test.yml`, `.pr-test.local.yml`, `.pr-test.local.yml.example`, `hack/proctor-seed-local.sh`, `.proctor/runs/`). Hard to tell what PRoctor owns vs. what's project-specific.

v0.4.0 consolidates everything PRoctor-owned under a single `.proctor/` directory at the repo root. GitHub workflow file stays at `.github/workflows/proctor.yml` because GitHub Actions requires that path; everything else moves:

**Path map (v0.3.x → v0.4.0):**

| v0.3.x | v0.4.0 |
|---|---|
| `.pr-test.yml` | `.proctor/config.yml` |
| `.pr-test.local.yml` (gitignored) | `.proctor/local.yml` (gitignored) |
| `.pr-test.local.yml.example` | `.proctor/local.yml.example` |
| `hack/proctor-seed-local.sh` | `.proctor/seed-local.sh` |
| `.proctor/runs/<run-id>/...` (unchanged) | `.proctor/runs/<run-id>/...` (unchanged) |
| `.github/workflows/proctor.yml` (unchanged — GH requires path) | unchanged |

The consumer's `.gitignore` flips from:
```
.pr-test.local.yml
.proctor/runs/
```
to:
```
.proctor/local.yml
.proctor/runs/
```
(everything else under `.proctor/` is committed: `config.yml`, `local.yml.example`, `seed-local.sh`).

### Backwards compatibility (one-version shim)

`schema.load_config` reads the new paths first, falls back to the legacy paths with a deprecation warning printed to stderr:
```
[proctor] WARNING: reading legacy config at .pr-test.yml; v0.4.0 moved to
.proctor/config.yml. Re-run /proctor:proctor-init to migrate (it'll `git mv` the files).
```

The fallback exists so consumers can upgrade the plugin without their first run failing — but the warning is loud enough that "I'll migrate later" doesn't get forgotten.

### Migration via the wizard

`/proctor:proctor-init` gains a new pre-flight step (before the normal MODE branching): if it detects `.pr-test.yml` AND no `.proctor/config.yml`, it offers via AskUserQuestion:

- **Migrate to v0.4.0 layout (Recommended)** — `git mv` the files into `.proctor/`, update `.gitignore`, then continue with the normal wizard flow re-evaluating against the new layout.
- **Keep current layout** — fall through to legacy-path mode (compatibility shim runs at every PRoctor invocation; deprecation warning fires).

The migration commands use `git mv` for tracked files so the consumer doesn't lose blame history; the gitignored `local.yml` is moved with plain `mv` (it was never tracked).

### What changed inside the plugin

Bulk path rewrite across:
- `scripts/schema.py` — `load_config` path priority + all `.pr-test.yml.*` validation error labels.
- `scripts/worktree.py` — copies `.proctor/local.yml` (was `.pr-test.local.yml`); mkdir parents to handle nested path.
- `commands/proctor.md`, `commands/proctor-init.md` — all path references + the wizard migration step.
- `agents/pr-test-executor.md` — screenshot/logs paths unchanged (`.proctor/runs/`).
- `skills/{analyzing,planning,executing,fixing,reporting}-pr-*/SKILL.md` — config + setup-file references.

Test fixtures (`tests/test_helpers.py`):
- `test_worktree_setup_copies_pr_test_local_yml` → `test_worktree_setup_copies_local_yml`. Creates `.proctor/local.yml` under the test repo root, asserts the worktree contains `.proctor/local.yml` (mkdir-parents path).
- All other tests pass unchanged at 170 (schema validation tests don't touch path strings — they validate config CONTENT, not file LOCATION).

### Tests
- 170 unchanged (test count). One test was renamed + body updated to match new paths; the test still validates the same behavior (worktree copies the dev's gitignored local config) at the new location.

### Why a minor version bump

This is a breaking change for consumers — the file paths in their repo change. The compatibility shim in `load_config` means v0.3.x-laid-out repos still work at runtime, but the wizard now offers migration as the first question, and the deprecation warning is hard to miss. The plugin internals deserve a `0.4` boundary; the `0.3.x` line was 41 releases of incremental fixes.

## v0.3.41 — 2026-05-14

### Wizard: confirm setup commands + env source before writing yaml

User feedback after a real run where setup auto-generation got the env-source file path wrong, causing the local dev server to start with mismatched config and produce a `chacha20poly1305: bad key length` gRPC handshake error.

The fix: `/proctor:proctor-init` now ASKS before baking setup commands into `.pr-test.local.yml`.

**New Step 7f — Confirm setup commands + env source** (`commands/proctor-init.md`):

- **7f.1**: search for candidate env-source files (`dev_env`, `.envrc`, `.env`, `.env.local`, `set-env.sh`, etc.) and AskUserQuestion which one the setup should `source` before starting the server. Options: top auto-detected candidate (Recommended), each other found candidate, "None — server doesn't need pre-sourced env vars", "Other" (free-text path). Stored as `ENV_SOURCE_FILE`.

- **7f.2**: render the FULL proposed `setup:` block in chat as a markdown YAML fence. AskUserQuestion with three options:
  1. **Use as-is — these look right** (Recommended) — store the commands as `SETUP_COMMANDS` (list of strings).
  2. **Customize — I'll write my own** — set `SETUP_COMMANDS` to the "user will fill in" marker. Step 8b / 8c-pre will emit a `setup:` block containing a single TODO line, and the wizard summary will say "edit `.pr-test.local.yml` before running `/proctor:proctor`".
  3. **Skip — leave `setup:` empty** — `SETUP_COMMANDS = []`. Same TODO + summary warning.

`SETUP_COMMANDS` is now the single source of truth. Steps 8b (`.pr-test.local.yml.example`) and 8c-pre (seed-script's yaml emission) USE the confirmed value — they no longer regenerate from detection at write time.

### Why this matters

The wizard's stack detection is heuristic-good but not always right. `dev_env` vs `.envrc` vs neither is repo-specific. The right docker-compose path (root `docker-compose.yml` vs `infra/compose.yml` vs `.docker/compose.dev.yml`) is also repo-specific. Silent auto-generation made failures opaque ("PRoctor's running my server, why isn't it talking to my backend?") because the dev never saw what commands were running. Now the dev confirms upfront and edits if wrong.

The "Customize" / "Skip" options exist for the case where the dev's setup is sufficiently unusual that the wizard's snippets can't capture it. Better to leave a TODO than bake something wrong.

### Tests
- 170 unchanged (wizard prose only; no schema or helper script logic changed).

## v0.3.40 — 2026-05-14

### Generalized "unexpected response → read source first" + test-data convention

Two related executor-contract additions, both responses to live-trace feedback.

#### Unexpected response → read source first (generalizes v0.3.39)

The v0.3.39 form-submit "no whack-a-mole" rule is a special case of a broader principle: when the system gives the executor a response that doesn't match `how:`'s expectation, the right reaction is NEVER "retry with a tweaked input" — it's "READ THE SOURCE that produced the response, classify the result honestly".

`pr-test-executor.md` gains a top-of-procedure section spelling this out for every tool:

| Tool | What "unexpected" looks like | What to read |
|---|---|---|
| chrome-devtools | Element not found, wrong text, unexpected redirect/validator | Handler / route / view / validator for the URL |
| bash | Non-zero exit on expected-pass, unexpected stderr | Script being invoked, or the program / package it runs |
| curl | 4xx/5xx vs 2xx mismatch, JSON shape wrong | Route handler / controller |
| lint-only | Grep/file check produced unanticipated result | The file being checked + the diff |

The procedure: capture actual state → trace to source → classify into one of three buckets — **(a) Diff bug** (`status: fail, reason: assertion`, evidence cites the wrong source line + PR intent), **(b) Planning gap** (`status: fail, reason: missing`, evidence cites code that's correct + test expectation that was wrong), **(c) Environment bug** (`status: skipped, reason: environment`, evidence describes the mismatch). The classification is what the report needs to be valuable; random retry-until-it-works gives the human zero signal about what's actually broken.

Forbidden anti-patterns (each one a real failure mode from previous runs):
- Click the same button again "to see if it works the second time"
- Retry curl with different headers "to see which the server wants"
- Modify the test to match what the code did, without reading why
- Treat an unexpected redirect as "must mean my action succeeded"
- Conclude `pass` because "nothing visibly broke" when the assertion target wasn't verified

The v0.3.39 form-submit section is now framed as a specialization of this general rule — the validator error from a partial-fill IS the response that needs investigation before any retry.

#### Test-data convention: `ai-test-` markers, not lorem ipsum

User flagged that the executor was using ambiguous values like `"test"`, `"foo"`, `"fixture-1"` in form fills. Records created by PRoctor live in shared dev / staging DBs alongside real records — these names are indistinguishable from human-created records and impossible to GC safely.

New convention codified on both sides:

- **Names / titles / slugs**: `ai-test-<resource>-<short-item-id>` — e.g. `"ai-test-image-reward-t007"`. The item-id suffix keeps two runs distinguishable.
- **Emails**: `ai-test+<item-id>@proctor.example.com`.
- **URLs**: `https://ai-test.example.invalid/<slug>` (`.invalid` TLD is reserved for tests).
- **Slugs**: `ai-test-<item_id>` (idempotent retry support — same item always uses same slug).
- **Prices / amounts**: an obvious outside-real-range value (`99999.99` or `0.01`).
- **Descriptions**: `AI test record created by PRoctor item <item-id>`.
- **File uploads**: a 1×1 transparent PNG named `ai-test.png` — real bytes, clearly test.
- **Phone**: `+8100000000` (or country-specific test pattern).

Forbidden values: `test`, `foo`, `bar`, `asdf`, `1234`, lorem ipsum, real-looking names. If the form's validator rejects `-` or `+`, swap to the closest compliant pattern (`aitestimagerewardt007`) but keep the `aitest` prefix intact.

Both the executor agent and the planner skill enforce this — planner cites the recommended values in `how:` upfront, executor honors them; if planner omits the values, executor falls back to the convention.

### Tests
- 170 unchanged (this is agent + skill prose only).

## v0.3.39 — 2026-05-14

### Forbid form-submit whack-a-mole — read validator first, fill once, save once

User observed during a real run: executor on a HAPPY save item filled one field → clicked Save → got validator error → filled another field → clicked Save → got another error → looped. Reasonable behavior for an AI without code-reading guidance, but anti-pattern for testing:
- The test result conflates "did the save work?" with "did the AI eventually guess every required field?".
- Multiple intermediate validator errors pollute the evidence log.
- Slower (each save round-trip costs a 1-3s page reload + DOM update).
- Hides a real planning gap: if the test had to discover field N at submit time, the plan should have known about it.

This release codifies the right behavior across two contract documents:

**Executor agent** (`agents/pr-test-executor.md`):
- New section "For chrome-devtools items that submit a form, DO NOT play whack-a-mole":
  1. Read the validator source (cited in the item's `how:`) FIRST. Enumerate every required field, every type-driven conditional, every format check.
  2. Snapshot the live form. List every input/select with `required` attr / `aria-required` / asterisk / `*` glyph.
  3. Reconcile (1) + (2) into a single fill-plan with valid values for every required field.
  4. Fill all fields in one pass, click Save once.
  5. On unexpected validator error: ONE corrective fill + retry, flagged in `evidence` as a planning gap.
- Hard rule: cycling save → error → fill → save → error 3+ times → return `status: "fail"` with `reason: "whack-a-mole"` and evidence listing every validator error hit. The planner will see the cascade in the report and tighten the next round.
- Multi-step intentional flows (Save Draft → Edit → Publish, each a known-correct submit) are explicitly still allowed — the rule is "no iterative-trial on a single logical submit", not "no multi-stage workflows".

**Planner skill** (`planning-pr-tests/SKILL.md`):
- New section "Complex-form save items: cite the validator path in `how:`": for every chrome-devtools save item the `how:` MUST cite the validator file path. Helps the executor read fresh (validator might have shifted between planning and execution under auto-fix loops); without the citation the executor falls back to DOM-snapshot heuristics which catch fewer requirements.
- Path discovery hint: walk the ChangeMap hunks for files matching `*_validator.go` / `models/<resource>.go` / `app/models/<resource>.rb` / `admin_resource.go`, or whose `summary:` mentions "validator" / "validates" / "required". The v0.3.27 `error_signals.py` `validation` signals come from these.

### Tests
- 170 unchanged (this is agent + skill prose only; no schema or helper script logic moved).

## v0.3.38 — 2026-05-14

### Deprecate orchestrator hard-gate (6d) — the duplicate was stalling the AI

v0.3.37 trace: planning skill ran `plan_smells.py --strict` as its self-audit (v0.3.35+), found one warning (t-012 missing round-trip sibling), regenerated to 13 items, lint clean, returned to orchestrator. Then `✻ Baked for 5m 23s` — the orchestrator AI stalled before emitting the approval-gate table.

Diagnosis: v0.3.32 added `plan_smells` as a hard gate at orchestrator step 6d. v0.3.35 moved the SAME lint INTO the planning skill as its self-audit. Both checks ran — the same script, same exit code. The AI's mental model is "I already validated this, why am I running it again?" and it just sits there. Two layers of the same check is one too many.

This release removes the duplicate.

**Orchestrator** (`commands/proctor.md`):
- Step 6 is back to FOUR substeps: 6a header → 6b table → 6c estimate → 6d AskUserQuestion. (v0.3.33's renumbering to FIVE substeps is reverted.)
- Explicit note: "Do NOT re-run `plan_smells.py` at this stage; the planning skill already did. The v0.3.32/v0.3.33 'hard-gate at step 6d' design was deprecated in v0.3.38."
- New 6c-warn rare path: if `.proctor/runs/<run-id>/plan-smells.txt` exists and is non-empty (planning skill exhausted its 2 regen attempts and surfaced residual warnings), render them as a `### Plan smells (still present after 2 regen attempts)` section so the human reviewer sees what the skill couldn't fix. Same purpose the old 6d advisory fallback served, but now it's just a render — no script invocation.
- Top-of-file CRITICAL block updated: "step 6 has FOUR substeps" + explicit "do NOT re-run plan_smells here, the skill already did".

**Why this is safer than restoring v0.3.32's design**: v0.3.32's hard gate at the orchestrator level got skipped in real runs because the AI's "validate → emit → ask" mental model didn't include it. v0.3.35 moved the check inside the planning skill specifically because the skill boundary is harder to skip than orchestrator step prose. We trust that boundary; the orchestrator only consumes the artifact.

### Tests
- Unchanged at 170 (this is orchestrator prose only — no script logic moved).

## v0.3.37 — 2026-05-13

### Auto-checkout via worktree — chrome-devtools tests run against PR head, not user's branch

User trace on v0.3.36 showed an "ideal" plan (14 items, schema-clean, plan_smells-clean) get a result of 6 pass / 0 fail / **8 skipped** all with `reason: "branch-mismatch"`. The diagnosis was correct (local dev server was running user's `ci/proctor-v0.3-migrate` branch, not PR #1115's `mdx-12639-support-new-digital-reward-type`), but the friction was unacceptable: the user had to manually stash, fetch, checkout, restart their dev server, run PRoctor, then restore. For every PR they test.

This release adds automatic PR-aligned worktree management to the executor so chrome-devtools tests always run against the PR's code, without touching the user's working tree.

**New helper** (`plugins/proctor/scripts/worktree.py`):
- `setup --run-dir <dir> --pr-number <n> --head-sha <sha>` — creates `.proctor/runs/<run-id>/pr-checkout/` as a detached-HEAD worktree at PR's head SHA.
  - Fetches `pull/<n>/head` from origin if the SHA isn't already in local objects.
  - Verifies the fetched SHA matches the captured `pr.head_sha` — surfaces force-push corruption loudly instead of silently testing the wrong commit.
  - Copies `.pr-test.local.yml` from the original repo into the worktree (gitignored file, carries the dev's setup commands + credentials).
  - Detached HEAD avoids claiming the PR's branch name in the user's repo.
  - Idempotent: if worktree at the right SHA already exists, no-op. If at the wrong SHA (e.g. force-push between runs), tear down + recreate.
- `teardown --run-dir <dir>` — `git worktree remove --force` and unlink the marker file. Best-effort; failures don't abort the run.

**Executor flow** (`executing-pr-tests/SKILL.md`):
- Step 2 (newly numbered): align worktree before setup. If `git rev-parse HEAD == pr.head_sha`, skip the worktree entirely (`WORKTREE_DIR=$(pwd)`). Otherwise create one.
- Step 2b: setup commands run with `cwd=$WORKTREE_DIR`. Auth + chrome-devtools items inherit the same cwd.
- Step 5 (cleanup): teardown the worktree regardless of pass/fail. If user was already at PR head, this is a no-op.
- CI mode skips the worktree entirely — the CI test env is already deployed at PR head via the workflow.

### What changes for the developer

- No more `git stash + checkout + restart server + run + restore` dance.
- `/proctor:proctor <PR>` from any branch with any working-tree state Just Works™.
- The temporary worktree at `.proctor/runs/<run-id>/pr-checkout/` is the only directory PRoctor writes to outside its run dir. After teardown it's gone.
- Dev's `.pr-test.local.yml` is copied into the worktree — same auth + setup runs as before. Other gitignored files (`.env`, `node_modules/`, etc.) are NOT copied; if the consumer's setup needs them, declare them or rebuild from source.
- If the PR was force-pushed since PRoctor captured `pr.head_sha`, the fetch sanity-check fails loudly with a "was the PR force-pushed?" message rather than silently testing the wrong commit.

### Tests
- 163 → 170 (+7): worktree created at expected SHA, .pr-test.local.yml copied, missing local-yml is fine, idempotent on same SHA, recreate on different SHA, teardown removes worktree + marker, teardown no-op when marker absent.

## v0.3.36 — 2026-05-13

### plan_smells vocabulary expansion + reload-sibling self-flag fix

Acceptance-test subagent against v0.3.35 confirmed end-to-end correctness (clean plan, schema OK, both happy + round-trip + split negatives, "backend-dep-deferred" rationalization blocked). Subagent surfaced one usability paper-cut worth fixing now:

- The "all-negative plan" coverage check keys off `_WRITE_PHRASES` regex. The subagent naturally reached for `persist` to describe a happy save, which wasn't in the regex; the check then (incorrectly) reported "0 happy save items" and triggered an unnecessary regen iteration. The lint self-healed via the regen loop, but the warning text + vocabulary mismatch is friction worth removing.

**Vocabulary expansion** (`scripts/plan_smells.py`):
- Added past-tense forms (`saved`, `created`, `updated`, `submitted`, `edited`, `published`, `uploaded`) and three common synonyms (`persist`, `insert`, `store`, all tenses) to `_WRITE_PHRASES`.
- Warning text improved: instead of "0 items doing a happy-path save/create", it now enumerates the recognized verbs AND suggests rephrasing if the user has happy items using a synonym not in the list. Removes the "but I DID write happy items" confusion.

**Reload-sibling self-flag fix** (`scripts/plan_smells.py`):
- The past-tense additions exposed a latent bug: a reload sibling's `what:` legitimately contains past-tense write verbs as nouns (`Re-open saved record`, `Assert created record visible in list`). After v0.3.36 the regex matched those, and the round-trip check then flagged the reload sibling as itself needing its own reload sibling — an infinite-recursion-of-test-items absurdity.
- Fix: in the round-trip check, skip items that have `_RE_RELOAD` phrase in their own `what:` AND have `data_from` set (i.e. they're explicitly downstream of another item — they ARE the reload, not the source).

**SKILL.md vocabulary hint** (`planning-pr-tests/SKILL.md`):
- Added a line to the "Coverage balance" section listing the recognized write verbs so the planner uses one of them first time and skips the regen cycle.

### Tests
- 162 → 163 (+1): regression test for "reload sibling with past-tense write verb is NOT self-flagged" — the failure mode the past-tense addition would have introduced if not for the data_from + reload-phrase exemption.

## v0.3.35 — 2026-05-13

### Lint gate moved INTO the planning skill, new all-negative check, parse_pr_arg URL tolerance

Real-world v0.3.33 trace against PR #1115 showed THREE compounding failures:
1. **The orchestrator AI skipped step 6d hard-gate lint AGAIN** (despite the v0.3.33 rename). The AI uses a cached "validate → emit table → ask question" mental model and doesn't consult `commands/proctor.md` per step.
2. **The plan regressed to ALL NEGATIVE** — 5 chrome-devtools items, every one a validator-reject. AI rationalized: "PR body mentions backend dep not deployed, so happy save is deferred." This pattern slips past plan_smells: no save items means no round-trip-sibling check fires.
3. **`parse_pr_arg` rejected `/changes` URL suffix** — user copy-pasted from GitHub's "Files Changed" tab. Same failure for `/files`, `/commits`, etc.

This release fixes all three.

**Move the lint gate from orchestrator → into the planning skill itself** (`planning-pr-tests/SKILL.md`):
- The skill's LAST step is now: run `plan_smells.py --strict`. Exit 0 → return. Exit 1 → regenerate (max 2 retries) addressing each warning, then re-validate. After 2 failed regens, write a WARNING log line and return as-is.
- This makes the lint impossible to skip at the orchestrator level — by the time `commands/proctor.md` step 6 runs, the plan is already audited (or the skill has explicitly logged that audit failed).
- Explicit anti-pattern callout in the SKILL: "If you skipped happy-path saves because backend-dep deferred — STOP. Plan the happy save anyway with `tool: \"skip\"` + rationale so the gap is visible, not silently absent."

**New `plan_smells.py` check: all-negative plan** (`scripts/plan_smells.py`):
- Fires when: ≥2 negative items (`error_type` set OR `what:` matches negative phrases) AND 0 chrome-devtools items doing a happy-path write (save/create/update/submit/edit/publish/upload).
- Warning text guides the planner: "PRs that add new fields/forms need AT LEAST ONE happy item that fills the form with valid input and asserts the record persisted. If backend dependencies block the full save flow, plan the item anyway with `tool=\"skip\"` and `reason=\"backend-dep-not-deployed\"`."
- This catches the exact failure mode the user observed in 3 successive runs.

**`parse_pr_arg` URL tolerance** (`scripts/pr_fetch.py`):
- Now accepts `/files`, `/changes`, `/commits`, `/checks`, `/conversation` suffixes (the common tab-link patterns from GitHub UI copy-paste).
- Also tolerates trailing slash and `#issuecomment-...` anchors.
- Bare PR URL (no suffix) continues to work.

### Tests
- 150 → 162 (+12): 9 parametrized URL-suffix tolerance, 3 plan_smells coverage cases (flagged, not-flagged-when-happy-present, not-flagged-for-single-negative).

## v0.3.33 — 2026-05-13

### Hard-gate lint renamed step 6c-lint → step 6d (so the AI actually runs it)

User trace from v0.3.32 against PR #1115 showed the orchestrator AI **skipping the hard-gate lint entirely**. It went: stage 2 plan written → 6a header → 6b table → 6c estimate → straight to the AskUserQuestion. No `plan_smells.py --strict` invocation anywhere in the trace. The hard-gate safety net was invisible because the AI never engaged it.

Root cause: the v0.3.32 step was named `6c-lint` with a hyphenated suffix, sitting between proper-numbered substeps. The AI's mental model was "approval gate has substeps 6a, 6b, 6c, 6d" — `6c-lint` reads like an aside or footnote and gets skipped under attention pressure. Plus the prose at top-of-file said "after Stage 2 finishes → emit the approval-gate table to chat, THEN call AskUserQuestion" with no mention of the lint between them.

This release fixes both:

**Renumber** (`plugins/proctor/commands/proctor.md`):
- `6c-lint` → **`6d` HARD-GATE LINT** (a proper peer-numbered substep, no hyphen suffix).
- Old `6d` (AskUserQuestion) → `6e`.
- "Four sub-steps" → "FIVE sub-steps" with explicit "ignore stale memory of a four-substep version".
- Bash invocation block elevated to a fenced code block in 6d so the AI sees it as an actual command, not prose.
- "Do NOT" forbidden-shortcut list updated: explicit "Do NOT skip 6d" — running 6a→6b→6c→6e bypasses the safety net and ships unaudited plans.

**Top-of-file CRITICAL block** updated:
- "after Stage 2 finishes → emit table THEN AskUserQuestion" prose replaced with explicit "step 6 has FIVE substeps including 6d hard-gate lint; all five MUST execute in order".
- Adds an anti-stale-memory clause: if you remember a four-substep version, that's wrong, the count is 5.

### Bonus: `make_run_id` calling-convention doc fix

User trace also showed a `TypeError: make_run_id() takes 0 positional arguments but 1 was given` early in the run because the AI called `make_run_id(pr['number'])` positionally. The function is keyword-only (`def make_run_id(*, pr_number, head_sha, started_at_iso)`). The orchestrator prose just said "run-id from `runlog.make_run_id`" with no example. Replaced with a working snippet showing the kw-only call so the AI doesn't keep hitting this.

### Tests
- 150 unchanged (this release is orchestrator prose + minor doc fix, no script logic changes).

## v0.3.32 — 2026-05-13

### plan_smells from advisory → hard gate with bounded regeneration

A subagent dispatched against PR #1115 confirmed: a faithful planner reading the current SKILL.md produces a clean plan (split items, round-trip siblings, plan_smells empty). The user's live planner kept shipping the SAME defects across THREE attempts — combined happy+negative items, no round-trip siblings — even with the MANDATORY sections in SKILL.md and the advisory plan_smells output at the approval gate.

The diagnosis: **planner adherence, not rule clarity**. The advisory lint emits warnings, but nothing in the orchestrator forces a regeneration; the planner gets to ignore its own audit. This release closes the loop.

**CLI flag** (`plugins/proctor/scripts/plan_smells.py`):
- New `--strict` flag: exit code 1 when any warnings fire (default stays exit 0 for backward compat with anything that relied on advisory mode).
- Stdout unchanged in both modes (one warning per line).

**Orchestrator hard gate** (`plugins/proctor/commands/proctor.md` Step 6c-lint):
- Run `plan_smells.py --strict`.
- Exit 0 → proceed to AskUserQuestion. The gate is invisible when not triggered.
- Exit 1 → DO NOT show the approval gate. Read `.proctor/runs/<run-id>/regen-count.txt` (missing = 0):
  1. If < 2: write warnings to `plan-smells.txt`, increment regen-count, print `[proctor:plan] hard-gate triggered (attempt N+1/3); regenerating plan with smells feedback`. Re-invoke the `planning-pr-tests` skill with the change-map.json AND the warnings as explicit feedback — instructing the planner to split combined items and add round-trip siblings linked by `data_from`. Re-validate. Loop back.
  2. If >= 2 (third attempt failed): fall through to advisory mode for THIS run only — render the warnings, log `[proctor:plan] hard-gate exhausted regen attempts`, and let the human pick "Cancel — let me edit the plan first" at the approval gate.
- Cap at 2 regenerations to avoid infinite loops on planner pathologies.

### Why hard gate
- The user manually tested PRoctor 3 times against the same PR; each plan had the same combined-happy-negative defect. Three runs × human-in-the-loop time spent reading the table and noticing the defect is wasted effort.
- Mechanical regeneration with the warnings as explicit feedback is far cheaper than asking the human to spot the defect in a 9-row table.
- The 2-regen cap means worst-case cost is 3× the planner skill invocation; the fallback to advisory mode means the human still gets the final say if the planner truly can't comply.

### Tests
- 147 → 150 (+3): --strict exits 1 on warnings, --strict exits 0 on clean, default mode preserves exit 0 even with warnings.

## v0.3.31 — 2026-05-13

### De-hardcode SKILL examples — placeholders + rotated domains

User caught a real prompt-engineering bug: every concrete example in `planning-pr-tests/SKILL.md` (and a few in the orchestrator + other skills) used the mcd-website Digital Reward Image/Game PR's exact entities. The AI planner reads SKILL.md examples as **templates to mimic**, not as one-of-many-shapes illustrations — so plans for unrelated PRs were getting Image/Game-shaped scaffolds.

This release sweeps every example across the plugin and replaces them with EITHER:
- **Placeholders** (`<Resource>`, `<RequiredField>`, `<TypedField>`, `<role>`, `<list_route>`, `<field>`) for shape-template patterns — clearly marked as "fill these in with your PR's specifics".
- **Different-domain concretes** for worked examples — blog post drafts, user profile edits, saved search filters — chosen specifically because they DON'T match any obvious PR your AI would actually be testing. Rotation across domains forces the planner to extract the structural pattern rather than copy the entity names.

Files touched:
- `planning-pr-tests/SKILL.md`:
  - Coverage-balance worked example → placeholders.
  - Journey-first structured example → placeholders.
  - Journey-first prose example → blog post draft/publish (deliberately different domain).
  - Impact-radius regression phrasing → placeholder.
  - data_from cross-item example → placeholders.
  - produces + templates example → placeholders.
  - One-assertion-per-item split anti-pattern → placeholder pattern + two short examples from user-profile and saved-search-filter domains.
  - Write-persistence required-sibling example → placeholders.
  - preconditions example → placeholders.
- `analyzing-pr-changes/SKILL.md`: ChangeMap output example → placeholders.
- `reporting-pr-test-results/SKILL.md`: journey header example → placeholders.
- `agents/pr-test-executor.md`: outputs capture example → placeholder.
- `commands/proctor.md`: approval-gate plan-table example → placeholder rows.
- `scripts/impact_radius.py`: CLI example in docstring → placeholders.

Files intentionally left alone:
- `CHANGELOG.md` historical entries (v0.3.21 / .23 / .25 / .28 / .30) — those are HISTORICAL records of what triggered each release; rewriting them would lose context.
- `scripts/plan_smells.py` module docstring — the verbatim production plan it cites is the JUSTIFICATION for the script existing, not a template the planner reads.
- `scripts/schema.py` inline code comments — Python comments aren't read by the planner AI as examples.

### Why this matters
- The planner cannot distinguish "the documentation happens to use Image/Game" from "Image/Game is the canonical pattern". Every concrete example becomes a default mental model.
- Rotating domains across examples models the meta-rule: "the structure is what matters; substitute your domain's nouns."

### Tests
- Unchanged at 147 — the test fixtures continue to use the verbatim production strings (`Create reward type=Image: missing asset rejected; with asset, save succeeds`) so `plan_smells.py` is still pinned against the real-world phrasing it was built to catch.

## v0.3.30 — 2026-05-13

### Plan smells + mandatory round-trip sibling — hard-stop the "save without verify" regression

User re-ran PRoctor against mcd-website after v0.3.29 and the planner shipped this plan for a Digital Reward Image/Game PR:

```
t-008  Create reward type=Image: missing asset rejected; with asset, save succeeds
t-009  Create reward type=Game: empty + invalid Game URL rejected; valid URL saves
```

Two regressions in one observation:
1. **t-008 / t-009 collapsed happy + negative into one item.** The report shows one status per item; the happy half silently disappears when the negative half passes. The v0.3.21 "happy paths required" rule was satisfied in name (the words "saves" / "succeeds" appear in `what:`) but not in effect.
2. **No sibling item exists for round-trip data loading.** v0.3.22 said "append persistence checks to `how:`" — planner treats that as decorative and skips the round-trip. For a CRUD PR like this, "did the form save?" without "does the saved record load correctly?" is half a test.

This release ships a mechanical safety net AND tightens the planning rules so the safety net catches what the AI misses.

**Mechanical lint** (`plugins/proctor/scripts/plan_smells.py`):
- Reads a TestPlan, returns a list of advisory warnings.
- **Check 1: combined happy + negative phrasing.** When `what:` contains both a success word (`succeeds`/`saves`/`works`/`accepted`/`persists`) AND a rejection word (`rejected`/`invalid`/`error`/`fails`/`forbidden`/`missing`), AND the item does NOT declare `error_type` (which would signal intentional negative phrasing), emit `<id>: combines happy and negative phrasing — split into two items`.
- **Check 2: write without round-trip sibling.** When `what:` describes a write action (`save`/`create`/`update`/`submit`/`edit`/`publish`/`upload`), the tool is `chrome-devtools` (UI writes — bash/curl/lint-only don't need UI round-trip), `error_type` is unset (negative writes don't persist), and no other item references this one via `data_from` with `what:` containing a round-trip word (`re-open`/`reload`/`round-trip`/`loads back`/`navigates back`/`detail page`/`appears`/`visible in list`), emit `<id>: write action has no sibling item asserting round-trip data loading via data_from — add a follow-up item that re-opens the saved record`.
- Conservative pattern set: false positives intentionally minimized so the warnings stay trustworthy at the approval gate. Items with explicit `error_type` are skipped from both checks (their negative intent is declared).

**Orchestrator integration** (`commands/proctor.md` Step 6c-lint):
- Between the plan table (6c) and the AskUserQuestion gate (6d), the orchestrator runs the lint and emits warnings as a `### Plan smells (advisory)` section directly below the cost summary.
- Empty result → section omitted entirely (no empty-header noise).
- The reviewer sees the smells alongside the table when deciding "Run all" vs "Drop items" vs "Cancel — let me edit the plan first".

**Planning skill — two MANDATORY rules** (`planning-pr-tests/SKILL.md`):
- **"One assertion class per item"**: each item verifies EITHER success OR a specific failure mode, never both. Worked anti-pattern from the actual production plan included verbatim with the split version next to it.
- **"Write-operation persistence — REQUIRED separate item"** (replaces v0.3.22's "append to `how:`" guidance): for happy-path writes via `tool: "chrome-devtools"`, the plan MUST include a sibling item linked by `data_from` that re-opens the saved record and asserts all submitted fields round-trip through the read path. Two distinct pass/fail signals beat one bundled assertion. Mechanically checkable, so the orchestrator catches omissions; planner doesn't need to be perfect on the first pass.
- Explicit skip list: DELETE / pure validation rejects / `bash`+`curl` items / `lint-only` items — none of these need the sibling check.
- Explicit phrasing tips for sibling items so they pass the lint without contortions.

### Tests
- 135 → 147 (+12): clean plan returns no warnings, both production anti-patterns flagged (verbatim from user's run), pure negative not flagged, pure happy round-trip pair clean, write-without-sibling flagged, write-with-roundtrip-sibling clean, "appears in list" recognized as round-trip, negative writes (with error_type) not flagged, lint-only writes not flagged, bash writes not flagged, warnings sorted for stable output.

### What this leaves untouched
- The `produces` canonical vocabulary item (the "锦上添花" #2 from the previous discussion): still on hold pending real-world evidence of mixed-vocab collisions.

## v0.3.29 — 2026-05-13

### Active precondition verification — `verify_precondition_via`

User flagged a remaining gap after the 5-point quality review: `preconditions` was descriptive only — the executor displayed the string but didn't enforce it. When the starting state was wrong (e.g. DB had no Category records and the test edits one), failures got marked `fail` instead of "environment gap", forcing the reviewer to mentally separate "bug in the diff" from "PRoctor was run against the wrong env". This release closes that gap.

**Plan schema** (`schema.py`):
- New optional `verify_precondition_via: "<shell command>"` field on plan items. Non-empty string when set, null/absent otherwise.
- Templates (`{{<id>.<key>}}`) inside the command get the same cross-validation as `how:` / `preconditions` — orphan references rejected at plan-time.

**Executor flow** (`executing-pr-tests/SKILL.md`):
- Insert verification between template substitution and subagent dispatch.
- Exit 0 → environment matches; proceed.
- Non-zero exit / command-not-found → mark item `skipped` with `reason: "precondition-not-met"` and a clear evidence line including the command, exit code, and first ~200 chars of stderr. Do NOT dispatch the subagent.
- The skip is non-actionable from a diff-author standpoint — it signals "your environment is missing X, rerun against a properly seeded env", NOT "your code broke".

**Planning skill** (`planning-pr-tests/SKILL.md`):
- New "Active precondition verification" section explaining when to use the field: cheap (<1s) idempotent checks with clean exit codes, where vacuous test results would otherwise be confusing.
- Explicit distinction from `data_from`: the latter handles intra-run upstream-fail propagation; this handles inter-run environment gaps. Different mitigations.
- Anti-patterns called out: don't use it for "logged in as developer" (auth flow handles that), don't use it to ESTABLISH state (that's setup/seed territory), don't fuzzy-parse stdout (use exit codes).

**Reporter** (`reporting-pr-test-results/SKILL.md`):
- Skipped items now render in three distinct flavors instead of being lumped together:
  - `data-dep-failed: <id>` → `⏭ skipped (upstream <id> failed)` — intra-run sibling broke.
  - `data-template-missing: <id>.<key>` → `⏭ skipped (upstream <id> didn't produce <key>)` — producer contract violated.
  - `precondition-not-met` → `⚠ skipped (environment precondition failed)` — DIFFERENT visual (warning chevron, not skip arrow) because the action item is "fix env / reseed", not "fix the diff".

### Tests
- 129 → 135 (+6): field accepted, empty/null/non-string handling, template happy path, template with unknown key rejected.

### What this leaves untouched
- The "canonical produces vocabulary" item (the user's second remaining observation): explicitly NOT shipped. We weighed adding a Levenshtein-based "did you mean created_id?" hint and concluded a hint-without-enforcement creates CI noise without preventing the bug; the SKILL.md prose examples already model `created_id` / `detail_url` / `slug` consistently. If mixed vocabulary becomes a real-world problem (plans accumulating `id` / `record_id` / `new_id` collisions), revisit.

## v0.3.28 — 2026-05-13

### Structured journeys (#2) + impact_radius truncation flag (#5)

Final two of the user's 5-point quality review. After this release, all five are in.

#### Structured top-level journeys (#2)

v0.3.23 shipped journeys as a loose `journey: "<name>"` string on each item. Drawback: two items with `"create-image-reward"` vs `"Create Image Reward"` split into two report sections under string-match grouping; and the journey's `goal` + `terminal_state` (what the journey actually verifies) lived only in the planner skill prose, never reaching the report.

This release promotes journeys to a structured top-level array in the plan:

```jsonc
{
  "journeys": [
    {
      "id": "j-create-image-reward",
      "goal": "Admin creates a published Image-type digital reward.",
      "terminal_state": "Reward appears in /admin/rewards with status=Published; survives hard reload."
    }
  ],
  "items": [
    { "id": "t-001", "journey_id": "j-create-image-reward", ... }
  ]
}
```

- Schema: optional `journeys: [{id, goal, terminal_state}]` top-level; items reference via `journey_id`.
- Cross-validation: `journey_id` must exist in `journeys[]`; setting BOTH `journey` and `journey_id` is rejected (ambiguous for the reporter).
- Duplicate `id` values rejected; empty-string `id`/`goal`/`terminal_state` rejected.
- Legacy `journey: "<name>"` string still validates — backward compat for pre-v0.3.28 plans and consumers running mixed versions.
- Reporter (`reporting-pr-test-results/SKILL.md`): when items use `journey_id`, the journey header renders `### Journey: <name> — <pass>/<total>` PLUS `**Goal:** <goal>` and `**Terminal state:** <terminal_state>` lines. Reviewers see WHAT the journey was meant to verify, not just a name.

#### impact_radius truncation flag (#5)

v0.3.26's `impact_radius` capped at top 10 callers. Drawback for monorepos: a core util with 200 callers shows 10 in the radius, and the planner has no way to know the visible 10 don't represent the full blast surface.

This release adds a truncation signal:

- `impact_radius.py` helper returns `{"files": [...], "truncated": <bool>}`. `truncated` is `True` when MORE survivors crossed the threshold than `top_n`.
- ChangeMap hunk schema: optional `impact_radius_truncated: bool` (boolean only, no nullable).
- Analyzer skill: when helper returns `truncated: true`, **override the hunk's risk to `"high"`** before emitting (truncated fan-out means the visible 10 are a sample of a larger iceberg; risk should reflect the iceberg, not the sample).
- Planner skill: when `impact_radius_truncated: true`, plan **TWO** regression items instead of one — pick the highest-fan-out caller AND the next caller in a different subtree so one bad release-time choice doesn't miss whole packages. Both items inherit `risk: "high"` and cite the truncation in `rationale`.

**API note**: this is a breaking change for direct `collect_callers()` Python callers — the return type changed from `list[str]` to `{"files": list[str], "truncated": bool}`. Only the analyzer skill + tests were calling it, both updated in this release. CLI output also changed shape; pre-v0.3.28 consumers can pipe through `jq '.files'` for the old list-only shape.

### Tests
- 118 → 129 (+11): structured journeys (accepted, journey_id references, mixed journey+journey_id rejected, legacy string still validates, missing fields rejected, duplicate id rejected, empty string field rejected), impact_radius_truncated accepted, truncated flag type-checked, plus 3 truncation behavior tests in the helper (truncated-when-over-top_n, not-truncated-under-top_n, not-truncated-at-exact-boundary).

### Status of the 5-point quality review — 5/5 complete

- #1 impact_radius false-positive control ✅ (v0.3.26)
- #2 journey upgraded to top-level array ✅ (this release)
- #3 data_from artifact passing ✅ (v0.3.25)
- #4 error_type diff-pattern triggers ✅ (v0.3.27)
- #5 impact_radius_truncated flag + auto-risk-upgrade ✅ (this release)

## v0.3.27 — 2026-05-13

### error_type diff-pattern triggers — fix the "all-`validation` plan" bias (#4)

The `error_type` enum shipped in v0.3.22 but the planner over-emitted `validation` (validators are easy to spot in a diff) and under-emitted the rest — especially `state-conflict`, which manifests as small schema constraints and status-guard branches that don't visually scream "error handling". This release ships an unambiguous trigger system.

**Helper script** (`plugins/proctor/scripts/error_signals.py`):
- Scans hunk added-lines for diff patterns and returns `{error_type: [signal_names]}`.
- Patterns are CONSERVATIVE — false negatives are fine (planner falls back to inference); false positives are not (would make the planner plan items for non-existent failure modes).
- Coverage today: Go primary (handlers, GORM, gorm tags, sync primitives), Python, Ruby/Rails idioms, SQL DDL, generic HTTP status / error patterns.

**Pattern map (highlights):**
- **`state-conflict`** (deepest coverage, the hardest to spot): `CREATE UNIQUE INDEX`, `ADD CONSTRAINT ... UNIQUE`, `gorm:"uniqueIndex"` tags, `Version int` / `Revision int` field additions, `WHERE version = ?` clauses, `StatusConflict` / `ErrConflict` / `409` literals, `"already exists" / "duplicate"` error strings, `if order.Status != "..."` guards, `SELECT ... FOR UPDATE`, `sync.Mutex`/`RWMutex`, `idempotency_key` fields.
- **`permission`**: `IsAdmin`/`IsDeveloper`/`IsEditor`/`IsOwner`/`IsStaff` checks, `RequireRole` / `RequirePermission` / `policy.Allow` / `authorize!` / `cancan` / `enforce` calls, `StatusForbidden`/403.
- **`auth`**: `RequireAuth` / `RequireLogin` / `MustAuth` / `LoginRequired` / `authenticate_user!`, CSRF references, `StatusUnauthorized`/401, session lifecycle calls.
- **`not-found`**: `gorm.ErrRecordNotFound`, `if x == nil { return ErrNotFound }`, `StatusNotFound`/404, `render :not_found`.
- **`network`**: `http.Get/Post/NewRequest`, `Faraday` / `HTTParty` / `Net::HTTP` / `requests.get` / `axios`, `retry` / `backoff` / `with_retries`, `context.WithTimeout`, circuit breakers.
- **`validation`**: `validate(...)` calls, `validate:"..."` struct tags, `ValidationError` / `StatusBadRequest` / 400 responses, `validates_presence_of :email` / `validates_uniqueness_of` / Rails-style validation DSL, `yup` / `zod` / `joi` / `ajv` frontend validators.

**Planner skill** (`planning-pr-tests/SKILL.md` new section):
- Run helper against the hunk's added-lines BEFORE writing negative items.
- For every error_type the helper flagged → plan at least one matching negative item.
- Signal name goes into the item's `rationale` ("planned because the diff added gorm:\"uniqueIndex\" on Email").
- New "diff pattern → test recipe" lookup table per error_type, with state-conflict getting the deepest treatment (10 patterns mapped to 10 concrete test recipes).
- Helper-flagged-nothing fallback: planner can still infer one, leave `error_type` unset, explain in `rationale` why the helper missed it (feedback loop for tuning patterns).

### Tests
- 96 → 118 (+22): unique-index, gorm-unique-index-tag, version-field-added, version-where-clause, status-guard, conflict-response, select-for-update, idempotency-key, role-check-guard, forbidden-response, auth-middleware, csrf, unauthorized, gorm-not-found, nil-not-found-guard, http-client, timeout-config, validate-tag, rails-validates, no-match-returns-empty, signal-dedup, multi-error-type-in-one-diff.

### Status of the 5-point quality review
- #1 impact_radius false-positive control ✅ (v0.3.26)
- #2 journey upgraded to top-level array — queued
- #3 data_from artifact passing ✅ (v0.3.25)
- #4 error_type diff-pattern triggers ✅ (this release)
- #5 impact_radius_truncated flag with auto-risk-upgrade — queued

## v0.3.26 — 2026-05-13

### impact_radius — frequency-threshold filter drops single-import-line false positives (#1)

v0.3.24 shipped grep-based `impact_radius` but acknowledged in its own CHANGELOG that the regex approach mishits unrelated same-named identifiers. The most common false positive: an `index.ts` that does `export { Foo } from './foo'` (one match — a re-export, not a real caller). This release closes that case.

**Helper script** (`plugins/proctor/scripts/impact_radius.py`):
- Moves the grep + threshold + ranking logic out of the SKILL.md procedure (which was an AI-followed bash recipe) and into a tested unit.
- Uses `git grep -o` (per-MATCH output, not per-line) so multiple occurrences on a single line — `Foo(); Foo();` — count correctly.
- Aggregates cumulative match count per caller across all hunk identifiers.
- **Threshold: cumulative count ≥ 2.** A single match is overwhelmingly an `import { Foo } from '...'` line or a re-export. Filtering single-matches removes the dominant false-positive class without language-aware import resolution.
- Sorts by descending count, then path ascending for stability. Cap at top 10.
- Excludes the changed file, plus `*_test.{go,ts,tsx,js,jsx}`, `*.spec.*`, `tests/`, `test/`, `__tests__/`, `vendor/`, `node_modules/`, `dist/`, `build/`, `target/`, `.proctor/`.
- Tunable via flags: `--min-occurrences` (default 2), `--top` (default 10).
- Uses the `:(exclude)PATTERN` long-form pathspec — the shorthand `:!PATTERN` fails with `Unimplemented pathspec magic '_'` on directories like `__tests__/`.

**Skill** (`analyzing-pr-changes/SKILL.md` Step 7b):
- Procedure replaced with a single helper invocation: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/impact_radius.py --file FILE --idents "..." --repo .`
- AI no longer aggregates by hand. Eliminates per-run parsing drift.
- Distinguishes three outcomes: non-empty list → emit; empty list (analyzed, nothing crossed threshold) → emit `[]`; script failure → omit field entirely.

**Tunability rationale** — why threshold=2:
- TS/JS real caller: `import { Foo } from '...'` line + `Foo()` call line → 2 matches, passes ✓
- TS/JS re-export: `export { Foo } from '...'` → 1 match, filtered ✓
- Go real caller: import doesn't name the symbol; needs `x.Foo()` twice → 2 matches, passes ✓
- Python real caller: `from foo import Foo` + `Foo()` → 2 matches, passes ✓
- Single-mention comments / type annotations / docstrings → 1 match, filtered ✓
- Pathological one-liner with `Foo(); Foo();` on a single line → still 2 matches because `-o` counts per occurrence, not per line ✓

### Tests
- 86 → 96 (+10): real callers kept, single-match files dropped, changed file excluded from own radius, test files excluded, vendor/node_modules excluded, multi-identifier aggregation works, ranking by count, top_n cap, empty results, empty identifiers, min_occurrences tunability.

### What it does NOT fix
- Two unrelated symbols sharing an identifier name where the unrelated file uses the colliding name ≥ 2 times still slips through. That would require a compiler-grade import resolver (intentionally out of scope — the planner treats `impact_radius` as advisory). Future work could swap regex for tree-sitter for language-aware false-positive reduction.

### Status of the 5-point quality review
- #1 impact_radius false-positive control ✅ (this release)
- #2 journey upgraded to top-level array — queued
- #3 data_from artifact passing ✅ (v0.3.25)
- #4 error_type state-conflict diff-pattern triggers — queued
- #5 impact_radius_truncated flag with auto-risk-upgrade — queued

## v0.3.25 — 2026-05-13

### `data_from` artifact passing — producer outputs flow to consumer templates

User flagged the biggest landing gap in v0.3.23's `data_from`: it only said "skip downstream if upstream failed", but never wired the actual VALUES (record IDs, URLs) from producer to consumer. This release closes that gap.

**Plan-side contract** (`planning-pr-tests/SKILL.md` new section, schema):
- Producer items declare `produces: ["created_id", "detail_url"]` — list of output key names this item promises to capture. Keys must match `[A-Za-z_][A-Za-z0-9_]*` (used in shell-ish substitution).
- Consumer items reference values inline via `{{<producer_id>.<key>}}` syntax in `how:` / `preconditions`:
  ```
  preconditions: "Logged in as developer. Record at {{t-005.detail_url}} exists."
  how:          "Navigate to {{t-005.detail_url}}; click Edit; replace asset; Save."
  ```
- Whitespace inside braces tolerated for readability (`{{ t-005.created_id }}`).
- Canonical key vocabulary recommended (`created_id`, `record_id`, `detail_url`, `slug`) — keeps cross-journey reviews legible.

**Schema cross-checks** (`schema.py`):
- Every `{{<id>.<key>}}` in `how:` / `preconditions` must reference an item that's in this item's `data_from` AND that item must list `<key>` in its `produces`. Orphan templates rejected at validation, never make it to runtime.
- Duplicate keys in `produces` rejected.
- `outputs` on TestResults items: must be `{string-key: non-empty-string-value}`, keys match identifier pattern. Empty-string values rejected (a producer that returned `""` violated its contract).

**Executor flow** (`executing-pr-tests/SKILL.md` Step 4 + `pr-test-executor.md`):
- Maintains a `run_context: {item_id: outputs_dict}` accumulator during execution.
- BEFORE dispatching each item, walks `how:` / `preconditions` for `{{<id>.<key>}}` templates, substitutes from `run_context`. Missing key (e.g. producer passed but forgot to capture) → consumer marked `skipped` with `reason: "data-template-missing: t-007.created_id"`. Catches subagent contract violations BEFORE the dependent test runs against broken state.
- AFTER receiving subagent result: if the plan declared `produces: [...]` but the result's `outputs` dict is missing/empty for any declared key, override status to `fail` with `reason: "producer-missing-output"`. A producer that doesn't honor its contract is failed regardless of what the subagent thinks.
- Successful items' `outputs` go into `run_context` for downstream substitution.

**Subagent contract** (`pr-test-executor.md`):
- New `outputs` return field: REQUIRED when the dispatched item's plan entry has a non-empty `produces:` array. Values must be strings (URL paths, record IDs, slugs — never ints / objects / null).
- Explicit guidance on capture sources: extract from post-save URL, DOM data-attrs, response body — whatever's closest to "what the next item will navigate to".
- Templates in the item's `how:` / `preconditions` are ALREADY substituted by the executor when the subagent receives them — subagent doesn't see / interpret `{{...}}` syntax itself.

**Report** (`reporting-pr-test-results/SKILL.md`):
- Per-item section grows a **Captured artifacts** subsection rendering the `outputs` dict so reviewers can see exactly what flowed downstream.
- `data-template-missing` skips surface the missing `<id>.<key>` in **Failure reason** so the reviewer can jump to the upstream row.
- `producer-missing-output` failures render declared `produces:` keys vs. actual `outputs:` keys side-by-side.

### Tests
- 69 → 86 (+17): produces field acceptance / invalid-key rejection / duplicate-key rejection; template happy path / preconditions / whitespace tolerance / unknown-id / missing-data_from / key-not-in-produces / producer-with-no-produces; outputs accepted / null / non-string value / empty value / invalid key / non-dict-shape.

### Status of the 5-point quality review
- #1 impact_radius false positive control (regex → frequency threshold) — not in this release
- #2 journey upgraded from string to top-level array — not in this release
- #3 data_from artifact passing ✅ (this release)
- #4 error_type state-conflict diff-pattern triggers in SKILL.md — not in this release
- #5 impact_radius_truncated flag with auto-risk-upgrade — not in this release

## v0.3.24 — 2026-05-13

### `impact_radius` — grep-based caller analysis for regression coverage (#5)

Last of the 6-point planning-quality review. The analyzer now emits an `impact_radius` list per non-docs hunk; the planner reads it to size regression coverage proportional to blast radius.

**Analyzer** (`analyzing-pr-changes/SKILL.md` new Step 7):
- For each non-docs hunk, extract exported identifiers from added/modified lines (language-aware: Go `func/type/var/const` capitalized, TS/JS `export ...`, Python top-level `def/class` skipping `_*`, Ruby `def/class/module`).
- `git grep -l --untracked` for each identifier with word-boundary regex, excluding the changed file itself, test files, vendor/node_modules.
- Union per-identifier hits, sort by reference count desc + path lex asc for stability, cap at top 10. Cap candidates at 6 identifiers per hunk to bound grep cost.
- Emit per-hunk `impact_radius: ["caller_a.go", "caller_b.go"]`. Empty list = "looked, found nothing". Field absent = "didn't analyze" (docs hunks, languages we can't extract identifiers from).
- Explicitly NOT a compiler-grade resolver — false positives are fine, the field is a planner HINT, not a fact.

**Planner** (`planning-pr-tests/SKILL.md` new "Impact-aware regression coverage" section):
- Hunk with 5+ callers → add ONE regression item walking the most user-visible caller. Cite that caller in `rationale`.
- Hunk with 1–4 callers → optional extra regression item, only if the diff changed signature / return shape / side-effect contract.
- Empty list or missing field → no regression item.
- Phrasing convention: prefix `what:` with `REGRESSION: ...` so reviewers can scan.
- Explicit "don't explode N items per caller" rule — blast-radius signal is fan-out count, not "test every caller individually".

**Schema** (`schema.py:validate_change_map`):
- Optional `impact_radius` per hunk: list of non-empty strings, or null, or absent.
- Rejects self-reference (a hunk's own file in its own impact_radius is nonsense).
- Rejects empty-string entries.
- Backward compatible — pre-v0.3.24 ChangeMaps without the field still validate.

### Tests
- 63 → 69 (6 new): valid list, empty-list-allowed, null-allowed, non-list rejected, self-reference rejected, empty-string entry rejected.

### What's next
- 6/6 planning-quality items from the user's review are now in. Future work shifts to making the analyzer's identifier extraction smarter (currently regex; could become tree-sitter for fewer false positives), and giving the planner a way to surface "this regression item was generated because of impact_radius from <hunk>" in the report rationale automatically.

## v0.3.23 — 2026-05-13

### Journey-first planning (#1) + `data_from` cross-item state dependency (#4)

**User-journey backbone for plan organization** (`planning-pr-tests/SKILL.md` new section, `journey` field on plan items):
- Planner must derive 1–3 user journeys from the PR body + ChangeMap BEFORE writing items.
- A journey = goal + ordered steps + final-state assertion (think QA test cases, not isolated assertions).
- Each item carries `journey: "<name>"`. Items within a journey can depend on each other; items in different journeys are independent.
- Cap at 3 journeys (more = over-segmenting). Zero journeys is fine for typo/refactor PRs.
- Reporter groups items by journey: `### Journey: Create-Image-Reward — 3/4 in this journey`. HTML report opens a journey by default if any item in it failed.

**`data_from` field — strong state dependency** (schema + `executing-pr-tests/SKILL.md` + reporter):
- Plan items declare `data_from: ["t-007"]` when their meaningfulness depends on `t-007` having SUCCEEDED (not just finished). Distinct from `depends_on` which only orders execution.
- Schema enforces: every `data_from` entry must also be in `depends_on`. Self-reference rejected.
- Executor: if any `data_from` source has status `fail`/`skipped`, mark this item `skipped` with `reason: "data-dep-failed: <id>"`. Chain propagates.
- Reporter renders these distinctly (`⏭ skipped (upstream t-007 failed)`) so the reviewer knows the test was INVALIDATED, not opt-out skipped.
- Use cases: t-008 edits a record t-007 created; t-009 asserts an effect that t-008's action produces; etc. Don't use for items that share fixture data but not live test state.

### Tests
- 56 → 63 (7 new): journey field validation, data_from cross-ID validation, data_from-implies-depends_on enforcement, self-reference rejection, list-type enforcement.

### Queued
- #5 (impact_radius from import graph) — would need analyzing-pr-changes to grow grep-based caller analysis. Not in this release.

## v0.3.22 — 2026-05-13

### Planning quality — three QA-thinking rules (write persistence, preconditions, error variety)
User submitted a 6-point review of the planning skill. This release lands the three "low difficulty" items; the harder three (user-journey backbone, data_from dependency, impact_radius regression) are queued.

**1. Write-operation persistence (rule, no schema change)** — Every write item (form submit, POST/PUT/DELETE, state-changing click) must include "navigate away → navigate back → reload → assert state matches submitted". Toast/200/element-visible is the immediate response; persistence is the question reviewers actually care about. Rule + worked example in `planning-pr-tests/SKILL.md`.

**2. `preconditions` field on plan items (schema + skill)** — Optional non-empty string. Separates starting-state requirements ("logged in as developer; one published category seeded") from the test ACTION (`how:`). Stops the executor from having to infer starting state from a mixed step list, and gives the report a clean column for human reviewers to read. Skill mandates using it whenever the precondition is anything beyond "PRoctor is logged in".

**3. `error_type` field on negative items (schema + skill)** — Optional enum: `validation` / `permission` / `network` / `state-conflict` / `not-found` / `auth`. Distributes negative-test coverage across distinct failure-mode classes. Rule: among all negative items, at most ~2 share an `error_type` — if you have 4 `validation` items, replace 2 with the `permission` / `state-conflict` / `not-found` variants that the diff actually touches.

### Queued (not in this release)
| # | Item | Why deferred |
|---|---|---|
| 1 | User-journey backbone (preprocess: derive 1–3 journeys from PR body, organize items around them) | Bigger structural change to planning prompt; need to think about journey representation |
| 4 | `data_from: "t-007"` data dependency between items | Executor needs to carry artifacts (created record IDs etc) forward — touches both schemas + executor logic |
| 5 | `impact_radius` regression range from import-graph grep | Adds non-trivial analysis to Stage 1; could explode plan size if not capped |

### Tests
- 51 → 56. New: preconditions accepted/empty-rejected/null-allowed; error_type all-enum-values-accepted/unknown-rejected.

## v0.3.21 — 2026-05-13

### Planning: happy-path tests are required, not optional
- **User feedback**: PRoctor planned 4 chrome-devtools items for the Digital Reward type=Image/Game PR — all 4 were validator-rejects-bad-input tests. Zero happy-path tests verifying the feature actually saves and publishes a valid Image / Game reward. "You only tested failure cases."
- **Why it happened**: the planner gravitated toward concrete, deterministic assertions. Negative cases ("expect 422 with this error string") are easier to write than happy cases ("submit form → success toast → record visible in list → render correctly"). Without explicit guidance to balance, the AI's distribution skewed all-negative.
- **Fix** (`planning-pr-tests/SKILL.md`, new "Coverage balance" section):
  - For every new behavior in the PR, at least one happy-path item is REQUIRED.
  - Negative items are useful but secondary, capped at ~1 per validator-branch.
  - Concrete worked example showing the all-negative plan vs the balanced 2-happy-2-negative plan.
  - Strong rule: "if you find yourself writing 4 chrome-devtools items and all 4 are submit-with-bad-input, STOP — replace one or two with the corresponding submit-with-good-input variant."

### Why this matters
A test plan that passes its 4 negative items + nothing else has verified the cage is locked but nothing about whether the building works. Reviewers need both signals.

## v0.3.20 — 2026-05-13

### Orchestrator: cement "no stopping between stages" with concrete sub-steps
- **Bug**: even after v0.3.14/v0.3.19's "do not pause" language, the AI still stopped after writing `test-plan.json` and validating it. Pattern was: write file → emit `[proctor:plan] done — 10 items planned` → end turn. The user had to manually type `继续` after each stage. Reported by zealot@theplant.jp.
- **Root cause**: "Then immediately proceed to..." is soft — when the AI's "concrete tool calls left to make in this turn" list goes empty after the schema-validate Bash, the turn naturally ends. The status line registers as a "task complete" signal.
- **Fix** (`proctor.md` top-of-file + Stage 6):
  - **New "Your turn ends ONLY when..." section** — enumerates the four legitimate end-states explicitly. If none has happened, the turn must continue. The status line is now explicitly called out as NOT a stopping signal.
  - **Per-stage continuation rules** stated as "Specifically, after Stage N finishes → invoke <next concrete tool call> with no pause." Names the next tool call instead of "proceed".
  - **Approval gate broken into four numbered sub-steps** (6a header, 6b table, 6c summary line, 6d AskUserQuestion). Each is a single concrete action in the same assistant turn. Combined directive: "Do all four. Do not stop between them."

After this, "Plan completed but no further execution" should be impossible — the AskUserQuestion call is enumerated as a required action of Stage 6, not a vague follow-up.

## v0.3.19 — 2026-05-13

### No more JSON walls in chat — stage artifacts go to disk, status lines go to chat
- **Bug**: at the approval gate, AI was dumping the full `test-plan.json` (~100 lines of JSON) into chat, THEN the AskUserQuestion. User saw the wall of JSON, mistook the wait-for-input for a hang, also couldn't actually parse the JSON visually to make a meaningful approval decision. Same pattern at Stage 1 — ChangeMap JSON dumped to chat. Both violate the v0.3.15 "render plan as markdown table" intent.
- **Fix** (`proctor.md` Stages 1, 2, and 6):
  - Stage 1: write `change-map.json` to disk, emit ONE status line to chat (`[proctor:analyze] done — <N> hunks, categories: <list>`). Never print the JSON.
  - Stage 2: write `test-plan.json` to disk, emit ONE status line (`[proctor:plan] done — <N> items planned`). Never print the JSON.
  - Stage 6 (approval): MANDATORY markdown table renders first, then the AskUserQuestion. New "DO NOT" list explicitly forbids skipping the table, printing JSON instead of the table, or collapsing the table into a one-line "3 lint + 5 ui — run?" summary.

After this, the approval gate looks like a clean table + a 3-option question, not "JSON dump + a question hidden underneath it".

## v0.3.18 — 2026-05-13

### Local mode gets a real HTML report (with screenshots, "why this test", auto-open)
- **User feedback**: "the local report isn't readable — no screenshots, explanations too thin, I can't tell what was tested or why."
- **Three changes**:

  **1. New `rationale` field on plan items.** Planning skill now writes WHY each test was generated for THIS diff (cites the relevant hunk / PR-body claim / risk category). Schema validates the field optionally. The reporter renders it as "Why this test" — the dev can audit the planner's reasoning, not just "trust me bro".

  **2. HTML report alongside the markdown** (`.proctor/runs/<run-id>/report.html`). Single self-contained file (CSS inlined, screenshots via `./screenshots/<id>.png` relative path). Features:
  - Sticky header with pass/fail counts + cost
  - Per-item `<details>` blocks — fail items default OPEN, pass items default closed
  - Five sections per item: Why this test / What it did / How / Result / Screenshot
  - Dark mode via `prefers-color-scheme` (no JS)
  - Click-to-zoom screenshots (native browser behavior)

  **3. Auto-open**: at the end of a local run, the reporter calls `open <report.html>` (macOS) / `xdg-open` (Linux). The dev gets the report in their browser without typing `open` themselves.

- **Stdout in local mode is now MINIMAL** — short summary + paths only. No more dumping the full markdown verbatim into chat; the HTML is the readable artifact, terminal output is just signposting.

### Why HTML over markdown for local
The PR-comment use case loves markdown because GitHub renders it nicely. Locally, terminals can't render images, can't collapse `<details>`, and look noisy. HTML in a browser solves all three with one extra file. The markdown still gets written (`.proctor/runs/<run-id>/report.md`) for anyone who wants to copy it into another tool.

## v0.3.17 — 2026-05-13

### Local report: file:// screenshots + dump full markdown + show paths
- **Bug**: local-mode report referenced screenshots with the CI-mode "in artifact" link template, which becomes a broken URL when no GitHub Actions run exists. The AI also tended to paraphrase the markdown into a brief "all good" summary instead of dumping the full report. Result: dev saw a stripped-down report with no images, far less detail than the PR-comment version. Reported by zealot@theplant.jp after a local run.
- **Fix** (`reporting-pr-test-results/SKILL.md`):
  - Screenshots in local mode use `file://<absolute path>` markdown image embeds — VS Code's markdown preview renders these inline. Also includes the path as a plain string so the dev can open in Preview / their IDE / Finder directly.
  - Log refs in local mode show the absolute path, not the misleading "in artifact" suffix.
  - Procedure step 5 explicitly mandates "dump the full rendered markdown verbatim — every `<details>` block, every per-item section. Do NOT summarize, do NOT skip sections, do NOT collapse pass items into a one-line 'all good'." The AI was treating the report markdown as a draft to summarize; it's actually the deliverable.
  - After the markdown, three follow-up lines tell the dev where the report.md / screenshots / patches dirs live in absolute paths, plus a hint to open `report.md` in VS Code's markdown preview for inline-rendered screenshots.

### Why
The PR-comment version of the report uses GitHub-hosted artifact URLs + GitHub-rendered markdown — looks polished. Local mode's terminal can't render images and won't auto-collapse `<details>`, but it can give the user accurate paths to open the assets themselves. The fix closes most of the parity gap.

## v0.3.16 — 2026-05-13

### Wizard: read the actual login template, don't hardcode qor conventions
- **Bug**: Step 7b pre-filled `auth.selectors.email = input[name="login"]` from qor/auth defaults. Most consumers override that template — mcd-website's form uses `name="email"`. Hardcoded selectors silently pass init, then login fails at runtime when PRoctor tries to fill a non-existent input. Reported by zealot@theplant.jp during a real /proctor:proctor run on mcd-website (had to inline-patch selectors during execute).
- **Fix**: Step 7b now greps the codebase for login-form templates (across `.tmpl`, `.html`, `.erb`, `.tsx`, `.jsx`, `.vue`, `.svelte`, `.go`), Reads each candidate, and extracts `name=` from the actual `<input>` elements. Classifies by attribute heuristics (`type="password"` → password, `type="email"` or name-matches-`email/login/username` → email, name-matches-`passcode/totp/otp/code` → totp, `<button type="submit">` → submit). Then confirms with the user via AskUserQuestion before baking into `.pr-test.yml`.
- **Fallback**: if no template is found (e.g. external SSO host), the wizard asks honestly instead of guessing.

### Why this matters
Every consumer customizes their login template at some point. The qor/auth defaults are a starting point, not a constant. The wizard's job is to read what's there, not assert what should be there.

## v0.3.15 — 2026-05-13

### Approval gate: render the plan as a markdown table, not a one-line summary
- **Bug**: at the local-mode approval gate, PRoctor was presenting "10 items: 3 lint-only + 2 bash + 5 chrome-devtools — Run all?" with no per-item visibility. The user couldn't tell what each item actually tested without `cat`-ing `test-plan.json` themselves or trusting the AI blindly. Reported by zealot@theplant.jp.
- **Fix**: the orchestrator now MUST render the full plan as a markdown table to chat BEFORE invoking AskUserQuestion. Columns: id / category / risk / tool / as_account / what (one-sentence). Below the table, a rough cost/time estimate.
- **AskUserQuestion simplified to a 3-way decision** (was implicit "uncheck unwanted ones" which didn't actually fit AskUserQuestion's multi-select shape for 10+ items):
  - Run all (Recommended)
  - Drop specific items (follow-up free-text question for IDs)
  - Cancel — let me edit the plan first (abort + invite hand-edit + re-invoke)

Now the user gets a scannable view of "what AI is about to do" and can intervene at item granularity.

## v0.3.14 — 2026-05-13

### Orchestrator: stop pausing between stages
- **Bug**: `proctor.md` listed stages 1–9 as sections without strong "DO NOT STOP between these" language. AI consistently treated each stage as a checkpoint — wrote the ChangeMap, then idled waiting for confirmation. User had to nudge it to continue. Same for "test-plan written → stop → user prompts → continue". This added 2–4 turns of dead-time per PRoctor invocation and broke the "set it and forget it" promise.
- **Fix**:
  1. New "Critical: this command runs the WHOLE pipeline non-stop" callout at top of `proctor.md`, listing the THREE legitimate pause points (hard error, approval gate, CI require_approval exit) and explicitly forbidding the chat-summary-after-each-stage pattern.
  2. End of each stage's section now has a one-liner "**Then immediately proceed to Stage N+1.** Do not pause." — gives the AI a clear continuation signal as it finishes each section.

After this fix, a local invocation should be: launch, see stage start markers stream by, hit the approval gate (only pause), see stages continue, see report. No "I just emitted the ChangeMap, want to continue?" detours.

## v0.3.13 — 2026-05-13

### Seed script: temp dir must live inside project tree (Go module resolution)
- **Bug**: v0.3.10's Go-stack `gen_hash` used `mktemp -d -t proctor-hash-XXXXXX` (with `-t`), which on macOS lands in `/var/folders/...`. The temp `main.go` there is OUTSIDE the project tree, so `go run` walking up for `go.mod` never finds the project's manifest and fails with `no required module provides package golang.org/x/crypto/bcrypt`. PRoctor self-diagnosed this when running its own scenarios on mcd-website and patched the seed script in place — that fix is being upstreamed.
- **Fix**: temp dir created INSIDE the project tree via `mktemp -d "./.proctor-hash-XXXXXX"`. `go run` walks up and finds the project's `go.mod`. `trap RETURN rm -rf` cleans up on normal exit; `.gitignore` adds `.proctor-hash-*/` for the Ctrl+C-doesn't-clean-up case.
- **Why this matters**: Go's module resolution requires the source file's containing-dir-or-ancestor to have a `go.mod`. There's no way to override that from the command line. The only options are:
  1. Put the temp source inside the project tree (chosen — minimal change).
  2. Commit a permanent helper at `hack/proctor-bcrypt/main.go` and build-then-run (more noise in the project, but no temp dirs).
  3. Use a different bcrypt source (Python — broken on macOS PEP 668; `htpasswd` — not always installed).
  
  Option 1 wins on simplicity.

## v0.3.12 — 2026-05-13

### Seed script: embed `setup:` block so PRoctor auto-starts the local server
- **Bug**: v0.3.6–0.3.11's seed script wrote `.pr-test.local.yml` with `base_url` + `auth.accounts` only — no `setup:` block. So `claude /proctor:proctor` against a localhost URL hit 502 (server not running) and asked the dev to start `go run main.go` themselves. Directly contradicted v0.3.3's design goal: "PRoctor auto-manages local server lifecycle, dev only edits code."
- **Root cause**: the stack-aware `setup:` block was correctly generated into the `.pr-test.local.yml.example` (Section 8b) but the seed script's YAML emission (Section 8c-pre) only wrote auth, no setup. The example file got bypassed because the seed script is the path users actually take.
- **Fix**: seed script's YAML emission now writes a `setup:` block using the same stack-aware template as the example file. Commands include docker-compose bring-up, pidfile-based kill-and-restart of the previous server, build, nohup launch, and a wait-loop on the login page. Wraps the multiline commands in a single-quoted heredoc so `$i` / `$(seq ...)` / `$(cat ...)` remain literal in the YAML (for PRoctor to expand at runtime, not at seed-script time).

### After this fix, the cycle is

```
1. claude /proctor:proctor-init           ← generates seed script (once)
2. ./hack/proctor-seed-local.sh           ← writes .pr-test.local.yml with setup:
3. claude /proctor:proctor <PR#>          ← PRoctor starts server, logs in, runs tests
4. edit code → repeat step 3 (no manual go run between iterations)
```

## v0.3.11 — 2026-05-13

### Seed script: bash 3.2 compatible (macOS default `/bin/bash`)
- **Bug**: v0.3.6–0.3.10's seed script used `declare -A` (associative arrays) — a bash 4+ feature. macOS ships bash 3.2 as `/bin/bash`; `#!/usr/bin/env bash` resolves to whatever's first in PATH, and many dev machines hit the system one. On bash 3.2, `declare -A` silently no-ops, then `[developer]=` is interpreted as INDEXED-array arithmetic indexing → `developer` evaluated as a variable → `set -u` errors with "developer: unbound variable". Reported by zealot@theplant.jp.
- **Fix**: rewrite to use parallel indexed arrays (`ROLES=(a b c)`, `EMAILS=(...)`, `SEEDS=()`) iterated by `for i in "${!ROLES[@]}"`. Works identically on bash 3.2 through 5.x. No platform-specific shebang gymnastics needed.
- **The TOTP seeds are now generated *inside* the loop** instead of upfront — so a partial-run failure leaves a clean state (re-run = fresh seeds, no half-written `.pr-test.local.yml`).

## v0.3.10 — 2026-05-13

### Seed script: use the project's own bcrypt, not Python's
- **Bug**: v0.3.9's `gen_hash` helper called `python3 -c "import bcrypt..."`. Python's bcrypt isn't a stdlib module — it requires `pip install bcrypt`. mcd-website's dev machine didn't have it; `./hack/proctor-seed-local.sh` errored on first run with `ModuleNotFoundError: No module named 'bcrypt'`.
- **Fix**: pick the bcrypt source based on detected stack:
  - **Go projects** (the case that broke): inline a tiny Go program using `golang.org/x/crypto/bcrypt` — the same library qor/auth uses, already in go.sum. `go run` against a temp file, hash to stdout.
  - **Node projects**: `npx -y bcrypt-cli`.
  - **Python projects**: try `import bcrypt`; if it fails, attempt `pip3 install --user bcrypt`; if THAT fails, print a friendly install hint.
  - **Other stacks**: try Apache's `htpasswd -bnBC 10 ...`; if not available, emit a comment explaining what to install.

### One-line workaround for already-broken installs
Devs who ran the v0.3.9 seed script and hit `ModuleNotFoundError` can either re-run /proctor-init to regenerate with the v0.3.10 helper, or unblock immediately with `pip3 install bcrypt` and re-run the existing script.

## v0.3.9 — 2026-05-13

### Seed script: wizard reads the code and writes the real SQL
- **Bug**: v0.3.6–0.3.8's seed script left `upsert_user()` as a TODO comment ("replace the SQL below..."). User correctly pointed out this is the wizard punting work back. PRoctor has the codebase right there — it should read the user model and write the actual SQL.
- **Fix**: new Step 8c-pre Read 0 (runs before the email/password reads). Wizard:
  1. Locates the user model file (user.go / admin_user.go / via gorm-tagged-struct grep).
  2. Reads it with the Read tool, extracts: struct name, `TableName()` override or gorm-default plural, columns for email/password/role/TOTP, whether `gorm.Model` is embedded.
  3. Reads migrations to verify column types + uniqueness constraints.
  4. Identifies password hashing scheme (bcrypt cost, argon2 params, qor/auth conventions). Inlines a `gen_hash` Python+bcrypt helper into the script — portable, doesn't assume Postgres has `pgcrypto`.
  5. Identifies TOTP secret storage format from how the app validates 2FA (pquerna/otp default = base32 → no conversion needed; other libs may differ).
  6. Assembles the actual `INSERT ... ON CONFLICT (...) DO UPDATE SET ...` statement with the discovered column names, and inlines it into the seed script.
- **Ambiguity handling**: if multiple candidate user models or multiple plausible columns are found, surface via AskUserQuestion *before* generating. No silent guess.

### Now the dev workflow is

```
claude /proctor:proctor-init     # wizard reads code, generates real SQL
./hack/proctor-seed-local.sh     # runs the SQL, writes .pr-test.local.yml
claude /proctor:proctor <PR#>    # tests against local
```

No more "fill in the TODO". The seed script works on first run.

## v0.3.8 — 2026-05-13

### Seed-script generation is now orthogonal to MODE
- **Bug**: When a v0.3.5 consumer re-ran `/proctor-init` to pick up a newer pin (`MODE=bump-only`), the wizard only patched the workflow version and skipped Step 8c-pre. The dev never got the seed script, never got `.pr-test.local.yml`, and the wizard's summary made it look like everything was already set up. Reported by zealot@theplant.jp.
- **Fix**: introduce a separate detection flag `NEEDS_SEED_SCRIPT` — true when `.pr-test.yml` declares `auth.accounts` and no `hack/proctor-seed-local.sh` / `scripts/proctor-seed-local.sh` / top-level equivalent exists. The flag is read in EVERY MODE; Step 8c-pre runs whenever it's set. So a `bump-only` consumer who installed PRoctor before v0.3.6 (when seed scripts didn't exist) now gets the script the next time they run the wizard for a version bump.
- **Idempotence**: if the seed script already exists, Step 8c-pre skips silently — never overwrites the dev's filled-in TODO block.

## v0.3.7 — 2026-05-13

### Wizard seed-script: detect email domain + password rules from the codebase
v0.3.6's seed script hardcoded `proctor-<role>@local.test` and `proctor-local-dev` as the password. Both were placeholders that the user explicitly called out as wrong: emails should match the project's domain convention (`ai-tester-developer@theplant.jp` style), and passwords need to satisfy the app's validator — `proctor-local-dev` would have been rejected by anything with a length≥12 or complexity rule.

- **Email domain detection.** Wizard greps existing user emails out of `README.md` / `CLAUDE.md` / `dev_env` / `dev_env_local` / git author history, picks the most-common-suffix as `EMAIL_DOMAIN`, then asks the user to confirm or override. Generated emails follow `ai-tester-<role>@<EMAIL_DOMAIN>` — descriptive (clearly an AI test account), domain-aligned (looks like a real internal account, which auth systems treat consistently).
- **Password rules detection.** Wizard reads the app's auth code for password constraints: `min_length`, complexity flags, hashing scheme (bcrypt / argon2 / etc.). qor/auth, devise, passlib, and stdlib bcrypt patterns are all sniffed. The user gets the detected rules and confirms — at which point the wizard generates a password that actually satisfies them, not a guess.
- **Both interactions are one-question-each** via AskUserQuestion — Recommended option pre-selected.

### Not changed
- Schema unchanged from v0.3.6.
- Other wizard steps unchanged.
- 51 tests still pass.

## v0.3.6 — 2026-05-13

### Added: inline auth credentials + local-seed helper script
Local dev shouldn't have to (a) manually create N AI-tester accounts in their local DB or (b) source env vars for credentials they already wrote down somewhere. v0.3.6 generates a seed script that does both.

- **Schema: `auth.accounts[].{email,password,totp_seed}` accept inline values**, as an alternative to `*_env` (env var name). Exactly one of inline-or-env required per field, never both. CI keeps using `*_env` (secrets), local config can use inline. Schema rejects mixed configs to avoid "I thought my env var was being used" surprises.
- **Wizard Step 8c-pre: generates `hack/proctor-seed-local.sh`** (or `scripts/` / top-level fallback). The script:
  1. Generates a fresh 32-char base32 TOTP seed per role.
  2. Upserts each role's user into the local DB. The actual SQL/code is project-specific — emitted as a clearly-marked TODO block with a Postgres + qor/auth template inline.
  3. Writes `.pr-test.local.yml` with **inline** credentials (no env-var indirection for local-only test accounts).
  4. Idempotent — re-running rotates seeds and refreshes the config.
- **`.pr-test.local.yml` stays gitignored** (already since v0.3.0). The seed script's output is explicitly DO NOT COMMIT.
- **Wizard summary** updated to surface the seed-script step alongside the existing CI secret-setup walkthrough.

### Tests
- 5 new tests (51 → previously 46): inline credentials accepted, mixed inline+env rejected, missing-both rejected, empty inline rejected, accounts can use different forms in the same auth block.

### Why
Mode B (browser handoff) was retired in v0.3.0+. The user pointed out: that leaves a gap — local dev still needs admin accounts to log into their localhost server, and asking the dev to do that manually is exactly the friction PRoctor was supposed to remove. Auto-seeding closes that gap.

## v0.3.5 — 2026-05-13

### Wizard role-discovery: Pass A is authoritative, Pass B annotates only
- **Bug**: v0.3.4 still missed `Role_internal_readonly` on mcd-website. Reason: the wizard's planner *did* read `roles.go` via Pass A but then intersected against `rolesPower` (Pass B's map-key extraction). Roles missing from `rolesPower` (read-only ones typically don't have a power value) got silently dropped.
- **Fix**: explicit "Pass A wins" semantics. When the roles file is found and any identifier was extracted, that set IS the complete list. Pass B is reduced to providing display annotations (e.g. `(power 6)` next to a role's checkbox label) — never to filter or shrink the list. Verification step added: after building `DETECTED_ROLES`, the wizard re-greps the roles file and confirms every `const` / `var` / enum member appears in the final set.
- **Preserve Pass A entries through filtering**: framework-keyword filter / id-substring filter / `^[a-z][a-z0-9_]*$` cleanup only apply to Pass-B-only entries. Anything Pass A identified is kept even if a filter rule would have dropped it.

### Why
The wizard's job is to read the role definitions correctly — not to second-guess them against a power table. A role that exists in code is a role.

### Not changed
- Schema / TOTP / executor / planner unchanged.
- All other wizard steps untouched.

## v0.3.4 — 2026-05-13

### Wizard role-discovery now catches multi-word snake_case
- **Bug**: the regex `\bRole_[A-Za-z]+\s*=` stopped at the first underscore *inside* the role name. So `Role_developer` matched but `Role_system_administrator` and `Role_internal_readonly` didn't. v0.3.2's discovery on `mcd-website` returned 3 of 5 roles. Reported by zealot@theplant.jp via screenshot.
- **Fix**: two-pass detection.
  - **Pass A (file-name-driven)**: find files named `roles.go` / `role.go` / `roles.py` etc. and *read each one* (Read tool, not regex), extract identifiers properly. Catches whatever pattern the project actually uses — including multi-segment snake_case, Ruby symbols, Python Enum members. Won't fail on edge cases the regex couldn't anticipate.
  - **Pass B (pattern grep)**: regex patterns rewritten to use word-char-permissive matchers (`[a-zA-Z0-9_]+` not `[A-Za-z]+`). Adds Go `rolesPower["..."]`-style map keys, TS enum bodies, Ruby `has_role :sym`, Python `class Role(Enum)` block parsing.
- **Filtering**: deduplicate, lowercase for picker display, strip framework noise (`role`, `permission`, `migration`, `id`, `key` substrings), require `^[a-z][a-z0-9_]*$` for clean rendering.

### Not changed
- All other wizard steps (7a/b/d/e, Section 8) unchanged.
- Schema, TOTP, executor SKILL.md unchanged.
- 46 tests still pass.

## v0.3.3 — 2026-05-13

### Wizard now generates auto-server-lifecycle for local dev
- **`.pr-test.local.yml.example` ships a stack-aware `setup:` block**, so when a developer runs `claude /proctor:proctor <PR>` locally, PRoctor brings up the dev server itself — no more "first start your server, then run PRoctor". Edit code → re-run PRoctor → automatic fresh-server cycle:
  ```
  claude /proctor:proctor 123
    → kill previous PRoctor-managed PID via pidfile
    → docker compose up + wait for DB port
    → go build / pnpm install / etc.
    → start server + wait for /admin to respond
    → form-login each configured account
    → run scenarios
  ```
- **Per-stack templates** for Node/Vite/Next, Go modules, GOPATH-era Go, and Python/Ruby (placeholder). Each starts the server with `nohup ... & echo $! > /tmp/proctor-<REPO_NAME>.pid` so a future invocation can kill it cleanly via pidfile — no `pkill` against patterns that might match the dev's other processes.
- **Health-check uses `auth.login_url`** as the wait-loop target. The login page rendering proves the binary booted AND templates resolve — useful smoke before scenarios even start.

### Executor SKILL.md clarification
- The merge-config + setup-runs-when-present-regardless-of-auth behavior is now spelled out explicitly. Previously the phrasing implied setup was for legacy mode only.

### Iteration cycle documented
- The example file embeds a comment block reminding the dev they don't need to manage `go run` / `pnpm dev` themselves between iterations — that's PRoctor's job now.

### Not changed
- Schema and TOTP helpers unchanged from v0.3.0.
- 46 tests still pass.

## v0.3.2 — 2026-05-13

### Wizard: discover admin roles from the codebase
- **Step 7c rewritten — no more "how many roles?" guess game.** Old flow asked the user to pick `1` / `2-3` / `4+` and then synthesized generic `AI_TESTER_ACCT<N>` env var names for anything past 3. That ignored the consumer's actual role taxonomy. New flow:
  1. Grep the codebase for common role-enumeration patterns (Go `Role_xxx`, TS enum/const, Python `class Role(Enum)`, Ruby `has_role :`, SQL/YAML seeds). Aggregate candidates into `DETECTED_ROLES`.
  2. Present a multi-select via AskUserQuestion: "I found these roles in your codebase — which should PRoctor test under?". Add an explicit "Add a role not in this list" option for the cases grep misses.
  3. If nothing's detected at all, fall back to a free-text loop ("type role name, `done` when finished").
  4. `MODE=migrate`: if existing `.pr-test.yml` already declares `auth.accounts`, default to keeping them; offer a "rediscover" path for consumers who restructured roles since their previous `/proctor-init` run.
- **Step 7d adapts to whatever Step 7c produced.** Env var names follow `AI_TESTER_<ROLE>_<KIND>` derived from the actual role names, preserving snake_case (e.g. `AI_TESTER_CMS_MANAGER_EMAIL`). A bulk-confirm shortcut lets users accept the entire convention without N rounds of "press enter to accept".

### Why this matters
Wizard previously assumed every consumer has the `developer / editor / viewer` trio and asked the count question as if the role list itself were unknown. Real admins have very different role taxonomies (mcd-website's `roles_manager/roles_models` has its own set; other consumers will have theirs). Grepping first means the consumer doesn't have to retype what's already in their code.

### Not changed
- Schema and TOTP helpers unchanged from v0.3.0.
- All other wizard steps (Section 7a/b/e, Section 8 file generation) unchanged.
- 46 tests still pass.

## v0.3.1 — 2026-05-13

### Wizard polished for existing-consumer migrations
- **New Step 0.5: detect existing PRoctor config.** Wizard reads `.pr-test.yml` and `.github/workflows/proctor.yml` before doing anything. Classifies the repo as `fresh` (no PRoctor yet), `migrate` (v0.2.x consumer, no `auth:` block), or `bump-only` (v0.3 consumer already on a slightly older pin). Each mode runs only the steps that matter — `bump-only` just patches the action pin and exits.
- **Workflow YAML is PATCHED, not regenerated.** Migration no longer overwrites `.github/workflows/proctor.yml`. Two surgical edits only: bump `zealllot/proctor/github-action@<old>` to `@<CURRENT_TAG>`, and insert/extend the `env:` block before `with:` to pass through every `AI_TESTER_*` secret. The user's `name:`, `on:`, `if:` guards, `services:`, custom steps, etc. are preserved untouched.
- **Section 7 rewritten in imperative voice.** Each sub-step is "Ask X via AskUserQuestion; if user picks A, do B. Save Y to memory." instead of the previous spec-style prose. Cuts a class of "the AI is reading this as documentation, not instructions" failure modes.
- **Migrate mode keeps existing settings.** `require_approval`, `auto_fix`, `mobile_emulator`, `per_test_timeout_seconds`, and any unknown keys from the existing `.pr-test.yml` are preserved (unknown keys land in a `# Preserved from previous .pr-test.yml:` block at the bottom of the new file). User is never asked to re-confirm settings they already chose.
- **`CURRENT_TAG` resolution unified.** Defined once in Step 0 by calling `gh release view --repo zealllot/proctor`. All later sections reference the same value. Eliminates the old failure where the wizard hardcoded a stale `@v0.2.0` because the AI copied the example markdown verbatim.
- **Hard refusal on prod URLs.** Step 7e (base URL question) refuses URLs matching `prod.`, `.qorcommerce.com`, or `www.<consumer-real-domain>` patterns. Not a warning — re-asks until the user provides a non-prod URL.

### Not changed
- No code changes in `plugins/proctor/scripts/` — schema and TOTP helpers from v0.3.0 are intact.
- All 46 tests still pass.

## v0.3.0 — 2026-05-13

### Added (major: existing-env mode + multi-role testing)
- **Auth block in `.pr-test.yml`** — `auth.type: form_with_totp` lets PRoctor log into the deployed test env (or developer's localhost) as a real admin, including 2FA. No more CI bring-up needed for runtime tests. Configure once: login URL, four CSS selectors (email/password/totp/submit), and an array of admin accounts. PRoctor performs the login flow before any chrome-devtools item runs.
- **Multi-account, role-aware planning + execution.** `.pr-test.yml.auth.accounts[]` declares each admin role you want PRoctor to test under (`developer`, `editor`, `viewer`, …). Each account has a `role_label` for planner context. Plan items can carry an optional `as_account: <name>` field to target a specific role; when omitted, the executor defaults to `accounts[0]`. The executor groups items by `as_account`, performs one login per group, and never lets cookies leak across roles. Especially valuable for diffs that change permission logic — the planner can emit the SAME check as multiple items with different expected outcomes per role.
- **`.pr-test.local.yml` overlay.** Per-developer overrides (different `base_url`, different env var names) live in `.pr-test.local.yml` and are gitignored. PRoctor deep-merges it over `.pr-test.yml` at load time. Dicts merge key-by-key recursively; lists REPLACE (the `accounts` array specifically — partial overlay of credentials would cause confusing silent fallthroughs). Matches the `.env` / `.env.local` pattern many tools use.
- **`scripts/totp.py` helper.** Pure-stdlib RFC 6238 TOTP code generator (HMAC-SHA1, 30-second step, 6 digits — Google Authenticator / Authy / 1Password / qor-auth defaults). Padding-tolerant for base32 seeds copied from QR codes. The executor calls this when a login form requires 2FA: `python3 $CLAUDE_PLUGIN_ROOT/scripts/totp.py "$SEED"`.

### Wizard
- **`/proctor-init` gains an "existing-env path".** New Q0 at the top asks "test against existing running server, or bring up a fresh server in CI?". The existing-env path skips DB / setup / Postgres detection entirely and walks a much shorter flow: login form selectors, number of admin roles, per-account env-var names, base URL. Generates a smaller `.pr-test.yml` (auth block only — no setup commands) plus a `.pr-test.local.yml.example` for developers to copy.
- Wizard automatically appends `.pr-test.local.yml` and `.proctor/` to `.gitignore`.
- Per-account secret walkthrough at the end: for every account, prompts user (in their own shell, never seeing the value) to set 3 secrets via `gh secret set`. Reminds them the TOTP value is the **base32 seed under the QR code**, not the 6-digit code.

### Schema additions (`plugins/proctor/scripts/schema.py`)
- `validate_pr_test_config(cfg)` — strict validation of the auth block when present (type, login_url, all four selectors, accounts array with unique names and required env var fields). Permissive when auth is absent (legacy/CI-bring-up mode unchanged).
- `validate_test_plan_account_refs(plan, cfg)` — second-pass check that every `item.as_account` references a real account name.
- `load_config(repo_root)` — loads `.pr-test.yml` and merges `.pr-test.local.yml` over it via `_deep_merge_overlay`.
- `validate_test_plan` now also enforces `as_account` is a non-empty string when set.

### Tests
- 16 new unit tests covering: legacy-mode config (no auth), full auth + multi-account, rejection of unknown auth type / missing selectors / empty accounts / duplicate names, plan account-ref cross-validation, deep merge semantics (dict recurse / list replace / scalar override), TOTP RFC 6238 test vector, base32 padding tolerance, 30-second step behavior. Total suite: 30 → 46.

### Not changed (backwards compat)
- Existing v0.2.x consumers without `auth:` in their `.pr-test.yml` see no behavior change. The legacy `setup:` / `base_url` flow still works for CI bring-up; the wizard still offers that path when explicitly chosen.

## v0.2.14 — 2026-05-11

### Fixed
- **`/proctor rerun` was checking out the wrong tree.** issue_comment-triggered runs use the workflow file from the default branch (correctly), but `actions/checkout@v4` with no `ref:` also defaults to the default branch — so rerun was analyzing master's code, not the PR head. Caught when `/proctor rerun` on `qor_demo` PR #3 (v0.2.13 pin) flagged the version pin as "still v0.2.12" — that was master's state, not the PR's.

  This had been broken since v0.2.3 introduced rerun; nobody noticed because the rerun trigger itself was also broken (workflow had to live on default branch — fixed in wizard caveat at v0.2.13) so live rerun was never exercised. Both bugs surfaced in the same validation session.

  Fix: move the Resolve PR number step before Checkout, capture the PR's `headRefOid` via `gh pr view`, and pass that as `ref:` to `actions/checkout@v4`. Now every event type checks out the PR head consistently.

## v0.2.13 — 2026-05-11

### Fixed
- **`/proctor-init` summary now warns about default-branch requirement for comment triggers.** Caught while validating `/proctor rerun` against `qor_demo` PR #1: the comment was posted, the workflow file existed on the PR branch, but no workflow run fired. Root cause is a GitHub-side rule — `issue_comment` events use the workflow file from the **default branch only**, so a wizard-generated `proctor.yml` that only lives on the feature branch silently does nothing for `/proctor run` / `/proctor rerun`. The wizard's final summary now flags this explicitly so consumers know to merge the workflow to main/master before relying on comment-driven flows.

## v0.2.12 — 2026-05-10

### Fixed
- **Schema validator no longer crashes on explicit `null` optional fields.** `validate_test_results` checked `if opt_str in item:` before requiring the value to be a string — so when the executor emitted `"screenshot_ref": null` for non-chrome-devtools items (which is the natural shape), the validator failed `isinstance(None, str)` and aborted the entire pipeline at stage 3 with `TestResults.items[i].screenshot_ref must be a string if present`. Caught when v0.2.11's first real CI run on `qor_demo` PR #1 reached execute (3 pass / 3 fail) but couldn't proceed to fix or report. Changed the predicate to `if item.get(opt_str) is not None:` so explicit-null is treated identically to omitted. Added a regression test (`test_test_results_explicit_null_rich_fields_accepted`).

## v0.2.11 — 2026-05-10

### Changed (BREAKING for local CLI)
- **Local `/proctor:proctor <PR>` is now local-only by default.** Previously the local CLI mirrored CI behavior — it posted a report comment to the PR and pushed a fix PR using the developer's git credentials. That made every iteration of pre-review testing spam the PR with comments and auto-open fix PRs from personal accounts. New defaults:
  - **No mutex acquisition** — the GitHub label lock is a CI-only coordination primitive; local runs skip it.
  - **No PR comment** — the report renders to terminal and saves to `.proctor/runs/<run-id>/report.md`.
  - **No fix push / fix PR** — patches are written as plain `.patch` files under `.proctor/runs/<run-id>/patches/<id>.patch` for the developer to review and `git apply --3way` themselves.
  
  CI behavior (post comment + push fix PR) is unchanged — `GITHUB_ACTIONS=true` keeps the old flow.

### Added
- **`--post-comment` and `--push-fix` opt-in flags.** For local runs that *do* want to post/push (e.g. running `/proctor` from your laptop because CI is down), pass these flags to restore the old behavior selectively.
- **`PROCTOR_MODE` / `PROCTOR_POST_COMMENT` / `PROCTOR_PUSH_FIX` env vars** propagated to all stage skills, so the orchestrator's mode decision is visible end-to-end.

### Fixed (caught by a real local CLI smoke run against `qor_demo` PR #1)
- **Documentation now uses `/proctor:proctor 123` consistently.** The bare `/proctor 123` form fails in `claude --print` mode with `Unknown command: /proctor` because the command name collides with the plugin name; Claude Code can only resolve it via the namespaced form. README and `docs/INTEGRATION.md` updated.

## v0.2.10 — 2026-05-10

### Fixed (all four caught by an end-to-end smoke run of the wizard against a clean fixture; PR-side testing missed all of them)
- **Workflow auth input name was wrong.** Wizard generated `claude_code_oauth_token` (underscores). The action's actual input name uses hyphens — `claude-code-oauth-token` — so the generated workflow would have been silently ignored on first PR run with `Unexpected input(s)` warnings. Updated the workflow template + added an inline note.
- **Action version pin was hardcoded to v0.2.0.** The model read `@v0.2.0` from the markdown's example and copied it verbatim instead of pinning to the actual current tag. Wizard now resolves `CURRENT_TAG` at runtime via `gh release view --repo zealllot/proctor --json tagName --jq '.tagName'` (with `gh api repos/.../tags` fallback, then literal `main`).
- **`/proctor rerun` trigger was missing from the workflow `if:` guard.** v0.2.3 added the rerun comment but the wizard's workflow template only checked `/proctor run`; consumers couldn't use rerun until they hand-edited the file.
- **App port was guessed silently when not derivable from code.** The wizard hardcoded `:7000` for an empty `main.go` smoke fixture. Step 1 now greps Listen/Run calls per language and falls back to a new Q2.6 open-ended question if no port is found, so consumers don't end up with a `base_url` pointing at a port their app doesn't bind.

## v0.2.9 — 2026-05-10

### Fixed
- **`/proctor-init` plugin now loads cleanly via `claude --plugin-dir`.** Local validation surfaced two bugs that PR-side testing missed: `plugin.json` used `owner` (rejected by the schema; the supported key is `author`), and `skills/fixing-test-failures/SKILL.md` had a YAML parse failure because the description's plain-scalar value contained `auto_fix: true` — the parser treated the inner `: ` as a mapping indicator and silently dropped the entire frontmatter at runtime. Quoted the description and renamed the manifest field. Validated with `claude plugins validate` and a real `claude --plugin-dir` smoke run that listed all 2 commands / 5 skills / 2 subagents.
- **Route A (compose-provisioned Postgres) now parses the actual compose file.** Stock defaults (5432/postgres/postgres) silently failed when the user's compose mapped to a non-default host port or used custom credentials — exactly what `qor_demo` does (host port 9722, user `qor_demo`). The wizard now uses `yq` to extract the host side of the port mapping and the `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` env values from the matching service, then injects those into the workflow's `env:` block. Wait loop changed from `docker compose exec ... pg_isready` to `nc -z localhost <port>` so it works on any service name and doesn't depend on the container being internally healthy first.

## v0.2.8 — 2026-05-10

### Added
- **`/proctor-init` now provisions Postgres in CI.** Three-signal detection — `docker-compose.yml` with a `postgres` image, DSN markers in `config/`/`database.yml`/`.env.example`, or a postgres driver import in source — flips the wizard into DB mode. A new conditional Q2.5 asks how to provision: reuse the existing `docker-compose.yml` (route A, default when compose is present), drop a GitHub Actions `services: postgres:` block into the workflow (route B, default otherwise), or skip and TODO it. The wizard captures `ENV_PREFIX` from any `configor.New(&configor.Config{ENVPrefix: "..."})` it finds, so the workflow's `env:` block (`<PREFIX>_HOST`, `<PREFIX>_PORT`, etc.) maps onto the consumer's existing config loader without code changes. If a `db/schema.sql` (or similar) is present, the wizard adds a `psql -f` setup step; otherwise it logs a TODO so the user remembers the DB is empty.

## v0.2.7 — 2026-05-10

### Changed
- **`/proctor-init` now detects GOPATH-era Go projects.** A repo with `*.go` source but no `go.mod` (e.g. `qor_demo` and other pre-modules codebases) used to fall through Q1 detection because the wizard only looked at `go.mod`. The wizard now walks `find . -maxdepth 3 -name '*.go' -not -path './vendor/*'` when `go.mod` is missing and, if hits found, tags the stack as `"Go (GOPATH-era, no go.mod)"` and pre-fills Q2 with the symlink-into-`$GOPATH/src/<owner>/<repo>` setup commands the toolchain needs.

## v0.2.6 — 2026-05-10

### Fixed
- **`set -u` no longer crashes on unset optional vars after fix stage.** v0.2.5's pipeline scriptified the body but kept `set -uo pipefail`. Vars that are only set inside conditional feature blocks (`PROCTOR_VISUAL_DIFF_PIXELS`, `VISUAL_URL_BASE`, `PROCTOR_USAGE_SUMMARY`, etc.) crashed the bash on `unbound variable` whenever the conditional didn't fire. First seen on `qor_demo` PR #1 (visual_regression off). Initialized all optional vars at the top of the script with `: "${VAR:-}"` defaults.

## v0.2.5 — 2026-05-10

### Fixed
- **GitHub Actions 21000-char per-expression limit no longer breaks the action.** v0.2.4's accumulated Run step (~40K chars) hit the limit on first use; first-time consumers got "The template is not valid... Exceeded max expression length 21000". Extracted the entire pipeline body to `github-action/scripts/run-proctor.sh`. The `run:` block is now a one-liner that execs the script. All `${{ github.* }}` references were replaced with env-var equivalents passed in via the step's `env:` block (`GITHUB_ACTION_PATH`, `GITHUB_REPOSITORY`, `GITHUB_WORKFLOW_REF`).

  No behavior change; only refactor for size.

## v0.2.4 — 2026-05-10

### Added
- **Visual regression diff (opt-in via `.pr-test.yml: visual_regression: true`).** Before the main pipeline runs, the action worktrees the PR base ref, runs `setup:` there, captures a full-page screenshot of `base_url` → `baseline.png`, tears down (configurable `teardown:` list, defaults kill common dev-server patterns), then proceeds with the PR head. After the head pipeline, captures `head.png` and runs `compare -metric AE -fuzz 5%` (ImageMagick) → `diff.png`. The report comment grows a "Visual regression" section: 3-image grid (baseline / diff / head) with differing-pixel count. Adds ~30–90 sec per run.

## v0.2.3 — 2026-05-10

### Added
- **`/proctor rerun [t-001 t-002 ...]` comment trigger.** After a failed PR run, instead of pushing an empty commit (which re-runs the entire pipeline), maintainers with write access can comment `/proctor rerun t-004 t-007` to re-execute only those items. The action downloads the previous run's artifact, hydrates plan + cached item results, drops result entries for the requested IDs, and runs only those. With no IDs (`/proctor rerun`), all items re-execute. Skips analyze + plan stages — saves both time and cost.
- **Skip directives in PR body.** Authors can drop HTML comments to filter what PRoctor plans:
  - `<!-- proctor:skip-paths vendor/ third_party/** -->` — analyzer drops matching hunks before classification.
  - `<!-- proctor:skip-categories docs cli -->` — drop hunks whose category is in the list.
  - `<!-- proctor:focus-paths src/payments/ -->` — whitelist; keep only matching hunks.
  - `<!-- proctor:max-items 5 -->` — soft cap on planner output.
  Schema accepts `pr_context.directives` as optional. Analyzer applies path filters at step 3 (before classify) and category filters at step 6.

## v0.2.2 — 2026-05-10

### Changed
- **Batched lint-only execution.** Previously, every test item — including trivial grep checks — got its own `claude --print` call, each re-loading the entire plugin context. PR #24's $1.44 run had ~470K input tokens, mostly from this duplication. The action now buckets items by tool: lint-only items go through ONE batched call, runtime items (chrome-devtools/curl/bash) still dispatch per-item in parallel. Estimated cost cut: 30–50% on grep-heavy PRs (most real PRs).
- **Per-stage cost breakdown in report.** The `**Cost:**` line is now followed by a `**Where:**` line: `analyze=$0.02 · plan=$0.03 · execute=$0.85 (5×) · execute-lint-batch=$0.01 (1×) · report=$0.01`. Tells consumers which stage burned the budget so they know what to tune.

## v0.2.1 — 2026-05-09

### Added
- **`/proctor-init` setup wizard.** Interactive slash command that detects the consumer's stack, asks 5 short questions (stack confirmation, setup commands, auto-fix on/off, run mode, auth method), generates `.pr-test.yml` + `.github/workflows/proctor.yml` pinned to the current PRoctor tag, walks the user through the auth secret (without ever seeing the value), and offers to flip the repo's Actions PR-creation permission via API. Total integration time: ~2 minutes vs. ~15 minutes manual. README and INTEGRATION.md now lead with this path.

## v0.2.0 — 2026-05-09

A batch of operational improvements based on lessons from running PRoctor across the fixture PRs.

### Changed
- **Parallel execute dispatch.** Per-item execute now runs at concurrency 3 (override via `PROCTOR_EXECUTE_CONCURRENCY`). A 7-item plan goes from ~34min to ~12min wall-clock. Cap protects against API rate limits and chrome-devtools port collisions.
- **Cost surfacing.** All `claude --print` calls switched to `--output-format json` so per-call usage is captured. `usage.jsonl` records every stage and item; the report header now shows `**Cost:** $X · N in / M out tokens` so consumers know what each PR run cost.
- **Planner detects `no test runner` stubs.** Skips planning `pnpm test`-style items when the `test` script is just an `npm init`-era stub (`echo "..." && exit 1`) or when no real runner is in deps. Same logic applies for Python (`pytest` not in `requirements.txt`), Go (no `*_test.go`), Rust (no `#[test]`), etc.
- **Screenshot retention.** `proctor-screenshots` branch keeps only the most recent N run subdirs (default 30, override via `PROCTOR_SCREENSHOT_RETENTION`). Old subdirs are git-rm'd before push so the branch doesn't grow unbounded. Trade-off: PRs referencing >30-runs-old screenshots get broken images; the original is still in the Action artifact.

### Added
- **Anti-loop guard.** Workflow runs on `fix-*-*` branches or PRs authored by `github-actions[bot]` / `proctor-*` short-circuit immediately (early step `steps.antiloop.outputs.skip == 'true'`). Prevents the recursion: fix → analyzed → fails → opens fix-of-fix → ...
- **`screenshot_focus` field on TestResults items.** Optional string the executor populates for chrome-devtools items pointing at WHICH region of the screenshot validates the evidence (or "verified via DOM only" when the assertion isn't visible). Renders below the image as "_What to look for:_". Catches the AC-1 failure mode where evidence checks `document.title` but the screenshot doesn't include the browser tab.

## v0.1.18 — 2026-05-09

### Changed
- **Per-item execute dispatch.** Previously the executing-pr-tests skill dispatched all subagents from a single `claude --print` invocation; with 5+ heavy chrome-devtools items the parent's context filled up and the model bailed with empty stdout (validated against `proctor-fixtures#21`). Now bash drives a per-item loop — each item gets its own focused `claude --print` call, with per-item retry. Aggregated into the canonical `test-results.json` after all items complete. Trade-off: more API round-trips, ~30s extra startup for a 7-item plan, but no more context-bloat bailout.

## v0.1.17 — 2026-05-09

### Fixed
- **Per-stage retry on transient claude failures.** Both v0.1.15 and v0.1.16 hit the same recurring transient: a stage returns non-zero with empty stdout after ~1.5min; the next run succeeds. Adding a 2-attempt loop inside `run_stage()` with 5s backoff. Also catches "returns 0 but writes nothing" via empty-file check.

## v0.1.16 — 2026-05-09

### Fixed
- **Screenshots push uses `git clone` + force-push.** v0.1.15 created the `proctor-screenshots` branch successfully but a subsequent run couldn't push back: `git fetch URL refspec` returned non-zero in the runner environment, the bash incorrectly fell through to "starting fresh", and the non-force push was rejected because the branch already existed. Replaced with `git clone --branch` (handles existing branch reliably) + `git push --force` (with retry).

## v0.1.15 — 2026-05-09

### Fixed
- **Nested double-quotes in stage 5 prompt no longer break bash.** v0.1.14 failed with `syntax error near unexpected token '('` because the report-stage prompt contained the literal `"(in artifact)"` inside a double-quoted `run_stage` argument; bash closed the outer string at the inner `"`, then tried to interpret `(in artifact)` as a subshell. Reworded to avoid all nested quotes.

## v0.1.14 — 2026-05-09

### Added
- **Inline screenshots via `proctor-screenshots` branch.** GitHub PR comments can't render images from Action artifacts directly. Between stages 4 and 5, the action now pushes any `<run-dir>/screenshots/*.png` to a long-lived `proctor-screenshots` branch in the consuming repo, indexed by run-id. The report skill embeds via `raw.githubusercontent.com` URLs.
- **Screenshot section is unconditional.** Previous template gated rendering on `status != pass`, so passing chrome-devtools tests never showed their screenshots. Now shown for any item with `screenshot_ref`.

## v0.1.13 — 2026-05-09

### Changed
- **Cache Claude Code install across runs.** `actions/cache@v4` around `~/.local/bin/claude` and adjacent share/config dirs. Install step skipped on cache hit. Saves ~10s/run.
- **Document toolchain caching for consumers.** `INTEGRATION.md` "Speeding up CI runs" section shows the standard `setup-go@v5`, `pnpm/action-setup@v3`, `setup-python@v5 (cache: pip)` pattern.

## v0.1.12 — 2026-05-09

### Changed
- **Rich per-item report sections.** Replaces the 5-column status table with `<details>`-collapsed sections per item: What it did / Evidence / Command / Output / Screenshot / Logs. Header now includes Action run + artifact links. Fail items default `<details open>`.
- **Schema accepts optional `command`, `output_excerpt`, `logs_ref`, `screenshot_ref`.** Type-validated as strings if present.
- **Executor now populates rich fields.** `pr-test-executor.md` documents required vs optional and the 4 KB cap on `output_excerpt`. `screenshot_ref` is REQUIRED for chrome-devtools items.

## v0.1.11 — 2026-05-09

### Added
- **Planner uses PR body context.** Analyzer surfaces `pr_context` (title, body, deduplicated links to Slack/Jira/Linear/Notion/Confluence/Figma, requirement_hints extracted from the body). Planner explicitly weights items toward documented requirements, phrases assertions in the body's wording, and writes `Per <ticket-id>: ...` for off-PR docs.
- **`pr_context` is optional in schema** — old ChangeMaps still validate.

## v0.1.10 — 2026-05-09

### Fixed
- **Tolerant fix stage.** A brittle test triggered the fixer; the fixer chewed for 5+ min and returned non-zero, killing the whole pipeline before the report comment could post. Stage 4 (fix) is now non-fatal — failure logs a warning, writes `null` to `fix-pr-ref.json`, and continues to stage 5. Report renders "needs human review" when no fix PR was opened.

## v0.1.9 — 2026-05-09

### Fixed
- **`logs_ref` is now optional on `TestResults` items.** In headless CI mode the executor reports inline and may not produce a per-item log file. The strict requirement was failing v0.1.8 stage-by-stage runs at validation time even when results were otherwise complete (e.g. fixtures PR #1: 2/2 pass with no log files written → SchemaError). `id`/`status`/`evidence` remain required.

## v0.1.8 — 2026-05-09

### Changed
- **Stage-by-stage bash orchestration in the GitHub Action.** The previous monolithic single-prompt invocation was non-deterministic — even with 3 retries, ~15% of fixture-PR runs would bail after stage 1. The model in `--print` mode treats its first complete-looking response as terminal regardless of how strongly the prompt warns against it.
  - Bash now derives `RUN_ID` deterministically (`pr<num>-<sha7>-<8hash>`).
  - Bash pre-fetches PR data and runs `setup:` commands before any `claude` invocation.
  - Each pipeline stage is a separate, focused `claude --print` call validated by `schema.py` before continuing.
  - The COMPLETE marker is emitted by bash when all stages produced valid outputs.
  - Failure semantics are sharper: each stage emits `PROCTOR_PIPELINE_FAILED stage=<name>` with a specific reason.

## v0.1.7 — 2026-05-09

### Fixed
- **Artifact upload no longer rejects run-id directories with colons.** The model occasionally improvised a UTC ISO timestamp (e.g. `2026-05-09T05:43:42Z`) into the run-id, breaking GitHub's `actions/upload-artifact` (which rejects `:` in path components). Two reinforcements:
  - Prompt explicitly tells the model to call `runlog.make_run_id` and includes the artifact-rejection rationale.
  - New "Sanitize run-id paths" step renames invalid chars to `_` defensively before upload.

## v0.1.6 — 2026-05-09

### Fixed
- **GitHub Action prompt no longer bails after stage 1.** The `--print`-mode invocation could non-deterministically return after only producing the ChangeMap, leaving stages 2–5 unrun. Reinforced in two ways:
  - Prompt now opens with an "ABSOLUTE RULE" framing the run as a single non-interactive task and adds an explicit "have all five stages printed?" self-check before emitting the COMPLETE marker.
  - The Run step retries the `claude` invocation up to 3 times if neither COMPLETE nor FAILED marker appears.

## v0.1.5 — 2026-05-09

### Changed
- **Planner prefers the cheapest tool that can verify each change.** New tier order: `lint-only` → repo's existing tests → `curl` → `chrome-devtools` → `skip`. Reads `.pr-test.yml setup:` to decide whether runtime-tier tests are even feasible; otherwise downgrades them to source-level grep checks and marks `risk: high`. Stops the "all skipped, no-server" anti-pattern.

## v0.1.4 — 2026-05-09

### Added
- **Stage gates and `PROCTOR_PIPELINE_COMPLETE` marker.** Each stage now prints a `[proctor:<stage>] done ...` line; the run is verified to have emitted the COMPLETE marker before the workflow turns green. Bash Verify step turns missing markers into clear job failures.
- Numbered, prescriptive prompt replaces the previous descriptive single paragraph.

### Fixed
- **bash quoting.** Previous prompt template used a Python f-string with backticks; bash command-substituted the backticks before claude saw them. Now uses a single-quoted heredoc + `sed` splice for `__PR__` / `__REPO__`.

## v0.1.3 — 2026-05-09

### Fixed
- **`gh pr comment` no longer stalls on permission prompts in CI.** Adds `--dangerously-skip-permissions` to the in-Action `claude --print` invocation. CI is already a trust boundary (workflow file is the gate, runner is ephemeral, credentials scoped to one PR), and `--print` mode cannot service interactive permission approvals.

## v0.1.2 — 2026-05-09

### Added
- **`claude-code-oauth-token` input** as an alternative to `anthropic-api-key`. Lets users with a Claude.ai subscription consume their subscription quota in CI via `claude setup-token`, without needing a separate Anthropic API billing account. Either input is accepted; at least one is required and the Action fails fast if neither is set.

## v0.1.1 — 2026-05-09

### Fixed
- **Slash-command invocation in `--print` mode.** `claude --print /proctor 1` returns "Unknown command: /proctor" — slash commands are not processed in non-interactive mode. The Run step now passes a prompt that describes the pipeline and the model applies the skills directly, with the plugin still loaded via `--plugin-dir`.

## v0.1.0 — 2026-05-09

Initial release. Covers spec §3–§11 (form factor, pipeline, contracts, edge cases, observability, testing strategy):

- Five-skill pipeline (`analyzing-pr-changes` → `planning-pr-tests` → `executing-pr-tests` → `fixing-test-failures` → `reporting-pr-test-results`) plus an explicit approval gate inside the slash command.
- Two subagents (`pr-test-executor`, `pr-test-fixer`) handle per-item work in isolated context.
- JSON contracts between stages (`ChangeMap`, `TestPlan`, `TestResults`, `FixPRRef`) validated by `scripts/schema.py`.
- Eight change categories: `frontend`, `api`, `schema`, `infra`, `mobile`, `cli`, `e2e-flow`, `docs`.
- Edge-case handling: force-push detection, setup failure, per-item timeout, fix-branch conflict, concurrent invocation mutex via `proctor:running` label, fork PRs, comments > 60 KB (gist fallback), `gh` rate-limit retry, `gh` not authenticated.
- 22 unit tests (`tests/test_helpers.py`), skill harness (`tests/run-skill.sh`), end-to-end harness skeleton (`tests/run-e2e.sh`).
- GitHub Action wrapper with `pull_request`, `pull_request_target`, `issue_comment` triggers; `/proctor run` resume gated on commenter write-access.

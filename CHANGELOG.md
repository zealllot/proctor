# Changelog

All notable changes to PRoctor are documented here. Versions follow semver: `v0.x.y` where `x` bumps on minor pipeline-affecting changes and `y` on action wrapper / packaging fixes.

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

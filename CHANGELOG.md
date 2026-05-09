# Changelog

All notable changes to PRoctor are documented here. Versions follow semver: `v0.x.y` where `x` bumps on minor pipeline-affecting changes and `y` on action wrapper / packaging fixes.

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

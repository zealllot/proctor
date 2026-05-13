# Changelog

All notable changes to PRoctor are documented here. Versions follow semver: `v0.x.y` where `x` bumps on minor pipeline-affecting changes and `y` on action wrapper / packaging fixes.

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

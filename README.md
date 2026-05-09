# PRoctor

> AI-driven PR test runner. Reads a PR's diff, plans tests, runs them, posts a structured report comment with screenshots — and opens a fix PR when something fails.

## Quick Start (60 seconds)

In the repo you want PRoctor on:

```bash
claude plugin add /path/to/proctor/plugins/proctor    # one-time
cd /your/repo
claude /proctor-init
```

The wizard asks 5 questions (your stack, server setup, auto-fix on/off, run mode, auth method), generates `.pr-test.yml` + `.github/workflows/proctor.yml`, and walks you through the auth secret. Open a PR — comment lands in ~10 minutes.

Live demo: every PR on [proctor-fixtures](https://github.com/zealllot/proctor-fixtures) is a working example. See [#21](https://github.com/zealllot/proctor-fixtures/pull/21) (admin visual change with screenshots) or [#18](https://github.com/zealllot/proctor-fixtures/pull/18) (5/5 chrome-devtools pass).

## What it does, end-to-end

```
Diff in        →  Categorize each hunk (frontend / api / schema / mobile / cli / e2e-flow / infra / docs)
.pr-test.yml   →  Plan one test item per behavior the diff or PR-body claims (lint-only / curl / chrome-devtools)
                  Setup commands run; per-item executor dispatches in parallel (concurrency 3)
Tests run      →  Each item produces evidence + command + output + screenshot
Failures       →  Fixer subagent generates a minimal patch; opens fix-<PR#>-<sha> PR
Report out     →  PR comment with collapsible per-item sections, inline screenshots, cost line, links to logs
```

Reads PR body for context — Slack/Jira/Linear links and acceptance criteria become test items, phrased in the body's wording.

## Local CLI (alternative to the wizard)

```bash
claude /proctor 123                                # PR number in current repo
claude /proctor https://github.com/org/repo/pull/123
```

PRoctor will print the test plan, ask you to approve (uncheck items you don't want), then execute.

## Manual setup

If `/proctor-init` isn't an option (e.g. you want to template a workflow without running an interactive command), see [`docs/INTEGRATION.md`](docs/INTEGRATION.md). Recipes for Node + Vite, Python + uvicorn, Go, Rust, multi-process stacks.

## Architecture

PRoctor is a Claude Code plugin. The pipeline is five skills + two subagents glued by a slash command:

```
analyze → plan → execute → fix → report
              (with PR-body context as test inputs;
               concurrency-3 per-item dispatch in execute)
```

Each stage is a Markdown SKILL.md with a JSON contract validated by `scripts/schema.py`. Subagents (`pr-test-executor`, `pr-test-fixer`) run isolated for per-item work.

| Component | What |
|---|---|
| [`commands/proctor.md`](plugins/proctor/commands/proctor.md) | `/proctor <PR>` orchestrator |
| [`commands/proctor-init.md`](plugins/proctor/commands/proctor-init.md) | Setup wizard for consumers |
| [`skills/analyzing-pr-changes`](plugins/proctor/skills/analyzing-pr-changes/SKILL.md) | Diff + PR body → ChangeMap (with `pr_context`) |
| [`skills/planning-pr-tests`](plugins/proctor/skills/planning-pr-tests/SKILL.md) | ChangeMap → TestPlan (cheapest tool first; reads PR body for ACs) |
| [`skills/executing-pr-tests`](plugins/proctor/skills/executing-pr-tests/SKILL.md) | TestPlan → TestResults (parallel per-item) |
| [`skills/fixing-test-failures`](plugins/proctor/skills/fixing-test-failures/SKILL.md) | Failed items → fix PR |
| [`skills/reporting-pr-test-results`](plugins/proctor/skills/reporting-pr-test-results/SKILL.md) | Markdown report posted as PR comment |
| [`agents/pr-test-executor`](plugins/proctor/agents/pr-test-executor.md) | One item → one result (incl. screenshot for chrome-devtools) |
| [`agents/pr-test-fixer`](plugins/proctor/agents/pr-test-fixer.md) | One failed item → minimal git patch |

## CI integration (GitHub Action)

See [`github-action/README.md`](github-action/README.md). Auth via either Anthropic API key or Claude.ai OAuth token (`claude setup-token`). Workflow triggers on `pull_request` and on `/proctor run` comments (for `require_approval: true` mode).

## Configuration reference

`.pr-test.yml` at the consuming repo's root:

```yaml
setup:                                  # Bash run before any test item
  - "pnpm install --frozen-lockfile"
  - "pnpm dev > /tmp/dev.log 2>&1 &"
  - "..."                               # Wait loop for readiness
base_url: "http://127.0.0.1:5173"       # For chrome-devtools / curl tests
test_focus: ["frontend", "api"]         # Hint to planner
require_approval: false                 # true = wait for /proctor run comment
auto_fix: true                          # Open fix PR on failures
fix_pr_target_branch: "${PR_BRANCH}"
per_test_timeout_seconds: 60
mobile_emulator: false
```

See [`examples/.pr-test.yml`](examples/.pr-test.yml) for an annotated reference and [`docs/INTEGRATION.md`](docs/INTEGRATION.md) for stack-specific recipes.

## Versioning + changelog

Pin the action to a tag: `zealllot/proctor/github-action@v0.2.1`.

[`CHANGELOG.md`](CHANGELOG.md) summarizes every release. Latest highlights: `/proctor-init` wizard (v0.2.1), parallel execute + cost surfacing + screenshot_focus (v0.2.0), inline screenshots via dedicated branch (v0.1.14).

## Design + plan docs

For implementers and contributors:

- Spec: [`docs/superpowers/specs/2026-05-09-proctor-design.md`](docs/superpowers/specs/2026-05-09-proctor-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-05-09-proctor.md`](docs/superpowers/plans/2026-05-09-proctor.md)

## Test PRoctor itself

```bash
# Unit tests for Python helpers + skill harness
python3 -m pytest tests/test_helpers.py -q
./tests/run-skill.sh analyzing-pr-changes frontend-only
./tests/run-skill.sh planning-pr-tests   mixed-stack

# End-to-end against the live fixtures repo (requires auth)
./tests/run-e2e.sh
```

## License

MIT (placeholder — confirm before publishing).

# PRoctor — Design Spec

**Date**: 2026-05-09
**Owner**: zealot@theplant.jp
**Status**: Draft → awaiting review

## 1. Purpose

PRoctor is a Claude Code plugin that runs AI-driven tests against GitHub Pull Requests. Given a PR, it analyzes the diff, plans appropriate tests, runs them autonomously (browser, API, CLI, etc.), opens a fix PR if anything fails, and posts a structured report back as a PR comment.

It exists as both:

- **Local CLI**: `claude /proctor <PR#>` — interactive, used by developers for self-review
- **GitHub Action**: triggered on PR open / label / comment — automated gate

The same skills, agents, and JSON contracts power both modes.

## 2. Non-Goals

- Not a generic test runner replacement (Jest/PyTest/etc. stay where they are)
- Not a coverage tool — focus is *verifying changed behavior*, not measuring %
- Not multi-platform (GitLab/Gitea/Bitbucket are out of scope)
- Not a CI orchestrator — it runs *inside* CI, doesn't replace it
- Does not push commits directly to the PR author's branch

## 3. Form Factor

A Claude Code plugin in the form below; no server, no daemon, no DB. The plugin itself lives under `plugins/proctor/` (Claude Code multi-plugin convention; mirrors the layout of the sibling `dsfix` repo).

```
proctor/
├── plugins/
│   └── proctor/
│       ├── .claude-plugin/plugin.json    # Claude Code plugin manifest
│       ├── commands/
│       │   └── proctor.md                # /proctor slash command entry point
│       ├── skills/
│       │   ├── analyzing-pr-changes/SKILL.md      # PR + diff → ChangeMap
│       │   ├── planning-pr-tests/SKILL.md         # ChangeMap → TestPlan
│       │   ├── executing-pr-tests/SKILL.md        # ApprovedPlan → TestResults
│       │   ├── fixing-test-failures/SKILL.md      # failures → FixPRRef
│       │   └── reporting-pr-test-results/SKILL.md # TestResults + FixPRRef → comment
│       ├── agents/
│       │   ├── pr-test-executor.md       # runs one test item, returns one result
│       │   └── pr-test-fixer.md          # generates a patch for one failure
│       └── scripts/                      # shared helpers (gh wrappers, schema validators)
│           ├── pr_fetch.py
│           ├── schema.py
│           └── runlog.py
├── github-action/
│   ├── action.yml                        # GitHub Action wrapper around `claude /proctor`
│   └── README.md
├── examples/
│   └── .pr-test.yml                      # repo-side config example
├── tests/
│   ├── fixtures/                         # diff JSON + config snapshots
│   ├── run-skill.sh                      # unit-level skill runner
│   └── run-e2e.sh                        # end-to-end against fixture repo
├── docs/
│   └── superpowers/{specs,plans}/...
└── README.md
```

## 4. Repo-side Configuration

`.pr-test.yml` lives at the root of the repo being tested. All fields optional; sensible defaults apply.

```yaml
setup: ["pnpm install", "pnpm dev &"]   # commands to prepare the test environment
base_url: "http://localhost:5173"        # entry URL for browser-based tests
test_focus: ["frontend", "api"]          # hint to planner: which categories matter most
require_approval: true                   # CI: wait for `/proctor run` comment before executing
auto_fix: true                           # open a fix PR when failures occur
fix_pr_target_branch: "${PR_BRANCH}"     # base for the fix PR (default: original PR head)
per_test_timeout_seconds: 300            # default 5min, override per-repo
mobile_emulator: false                   # set true to actually boot iOS/Android simulators
```

## 5. Pipeline

```
/proctor <PR-url|number>
        │
        ▼
[1] analyzing-pr-changes
    • gh pr view + gh pr diff
    • load .pr-test.yml
    • categorize each hunk → ChangeMap
        │
        ▼
[2] planning-pr-tests
    • ChangeMap → TestPlan (each item: {what, how, tool, risk})
    • category → tool mapping (table in §6)
        │
        ▼
[3] approval gate (in /proctor command)
    • local         → AskUserQuestion, user unchecks unwanted items
    • CI default    → skip approval; post plan to PR as announcement
    • CI + approval → wait for PR comment `/proctor run`
        │
        ▼
[4] executing-pr-tests
    • run setup commands from .pr-test.yml
    • dispatch each item to pr-test-executor subagent (parallel where independent)
    • collect TestResults
        │
        ▼
[5] fixing-test-failures (only if auto_fix=true and failures present)
    • dispatch each failure to pr-test-fixer subagent
    • collect patches → single fix branch fix-{PR#}-{shortsha}
    • gh pr create --base <PR head branch> → FixPRRef
        │
        ▼
[6] reporting-pr-test-results
    • render markdown summary (pass/fail/fixed table + fix PR link)
    • gh pr comment <PR#> -F report.md
```

The five `[1]..[5]` steps are independent skills with JSON contracts between them. Each can be re-run from cached input.

## 6. Change Categories & Tool Mapping

| Category | Trigger paths/extensions | Primary tool |
|---|---|---|
| `frontend` | `*.tsx`, `*.vue`, `*.svelte`, `*.css`, `*.html` | chrome-devtools MCP (page-level assertions) |
| `api` | handlers / controllers / routes, `*_handler.go`, `*Controller.*` | Bash + curl, or repo-defined test command |
| `schema` | `migrations/`, `*.sql`, ORM model files | Bash + migration dry-run + rollback check |
| `infra` | `Dockerfile`, `docker-compose*`, `.github/workflows/*` | Bash + `docker build` validation, `actionlint` |
| `mobile` | `*.swift`, `*.kt`, `ios/`, `android/`, RN screens | chrome-devtools mobile viewport + lint; native simulator only if `mobile_emulator: true` |
| `cli` | `cmd/*`, `bin/*`, `*_cmd.go`, executable entrypoints | Bash run binary, compare stdout/stderr/exit code, golden-file diff |
| `e2e-flow` | hunks span both frontend and api | chrome-devtools scripted user journey (login → action → assert backend state) |
| `docs` | `*.md`, `docs/`, comment-only changes | Skip execution; link lint + spell check |

Notes:

- `e2e-flow` is **additive**, not exclusive: a mixed PR yields its frontend tests + api tests + an extra e2e-flow test linking them.
- `mobile` does not boot simulators in CI by default (cost, flakiness). Override via `mobile_emulator: true`.
- `cli` uses golden-file workflow: first run is human-reviewed, subsequent diffs are surfaced as failures.

## 7. Component Contracts

| Component | Input | Output | Does NOT do |
|---|---|---|---|
| `analyzing-pr-changes` | PR url/number | `ChangeMap` JSON | plan, execute, measure coverage |
| `planning-pr-tests` | `ChangeMap`, `.pr-test.yml` | `TestPlan` JSON | fetch PR, execute, request approval |
| approval gate (in command) | `TestPlan`, mode | `ApprovedPlan` | execute |
| `executing-pr-tests` | `ApprovedPlan` | `TestResults` JSON | fix failures |
| `pr-test-executor` (subagent) | one test item | one result | persist, post to PR |
| `fixing-test-failures` | failure subset of `TestResults` | `FixPRRef` (or null) | push to author branch, write report |
| `pr-test-fixer` (subagent) | one failure + worktree | git patch + rationale | open the PR (skill consolidates) |
| `reporting-pr-test-results` | `TestResults`, `FixPRRef` | markdown body, posted as PR comment | trigger more tests |

### 7.1 JSON Schemas (informal)

```jsonc
// ChangeMap
{
  "pr": { "number": 123, "head_sha": "abc1234", "base_sha": "def5678", "url": "..." },
  "hunks": [
    { "file": "src/Login.tsx", "category": "frontend", "risk": "medium", "summary": "..." },
    { "file": "api/login.go",  "category": "api",      "risk": "high",   "summary": "..." }
  ],
  "categories_present": ["frontend", "api"]
}

// TestPlan
{
  "items": [
    {
      "id": "t-001",
      "category": "frontend",
      "what": "Login form rejects empty password",
      "how": "Open /login, submit empty, expect inline error",
      "tool": "chrome-devtools",
      "risk": "medium",
      "depends_on": []
    }
    // ...
  ]
}

// ApprovedPlan = TestPlan with `items` filtered by user/CI gate

// TestResults
{
  "items": [
    { "id": "t-001", "status": "pass", "evidence": "...", "logs_ref": ".proctor/runs/<run-id>/t-001.log" },
    { "id": "t-002", "status": "fail", "reason": "timeout", "evidence": "...", "logs_ref": "..." }
  ],
  "summary": { "total": 2, "pass": 1, "fail": 1, "skipped": 0 }
}

// FixPRRef
{ "number": 124, "url": "...", "branch": "fix-123-abc1234", "covers": ["t-002"] } // or null
```

## 8. Approval Modes

| Mode | Where | Behavior |
|---|---|---|
| `local` | `claude /proctor 123` in a terminal | `AskUserQuestion` lists each test item; user unchecks unwanted ones. |
| `ci-skip` (default in CI) | GitHub Action | Plan posted as PR comment for visibility; execution proceeds immediately. |
| `ci-approve` | GitHub Action with `require_approval: true` | Plan posted; runner exits. A new PR comment with body `/proctor run`, posted by a user holding write access on the repo, re-triggers the action and resumes from the execute step. The PR author specifically is not privileged — write access is the gate. |

## 9. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| PR force-pushed mid-run | `analyzing-pr-changes` records `head_sha`. Before `executing-pr-tests` runs setup, it re-fetches the PR and compares `head_sha`. Mismatch → abort + post "force-push detected, run aborted". |
| `setup` command fails | Abort run; report distinguishes "setup failure" from "test failure"; **no** fix flow (fixer can't fix env). |
| Test item timeout | Default 5min (per-repo override). Mark `fail` with `reason: timeout`; other items continue. |
| Fixer can't fix | Retry once. Still failing → report says "needs human" with reason; no fix PR opened for that item. |
| Fix branch already exists | Fetch, rebase. Rebase conflict → new branch `fix-{PR#}-{shortsha}-2`, PR description marks "supersedes". |
| Concurrent `/proctor` on same PR | Use PR label `proctor:running` as mutex. On entry: if label set → comment "already running, skipping" + exit. Always remove label on exit, including panic paths (trap/defer). |
| AI skipping the approval gate | Approval is a **command-level** explicit step. The `executing-pr-tests` skill is not loaded until the gate emits `ApprovedPlan`. Enforced by control flow, not skill self-discipline. |
| PR from a fork | `gh` reads diff fine. Fix PR uses standard "new branch in same repo, base=PR head ref" pattern — already the design. |
| Comment > 65,536 chars | Split: short summary as comment, detailed logs uploaded as a gist linked from the comment. |
| GitHub API rate limit | Honor `Retry-After`. Three failures in a row → abort with clear error; do not loop and burn quota. |
| Local: `gh` not authenticated | Pre-flight check (`gh auth status` + `gh repo view`). Print exact remediation command and exit. |

## 10. Cross-cutting Concerns

**Idempotency.** Each run keyed by `{PR#, head_sha, started_at}` as `run-id`. Cached at `.proctor/runs/<run-id>/` locally, or as a GHA artifact in CI. Re-runs reuse upstream cached stages (ChangeMap / TestPlan) when `head_sha` matches.

**Observability.** Every skill prints one structured log line at entry and exit:

```
[proctor:analyze] start  pr=123 sha=abc1234
[proctor:analyze] done   hunks=14 categories=[frontend,api]
[proctor:plan]    start  ...
```

Greppable from the Claude Code transcript without extra tooling.

**Secrets.** No tokens stored by PRoctor. `gh` CLI handles auth in both modes. The Action passes `GITHUB_TOKEN` via the standard `permissions:` block in workflow.

## 11. Testing Strategy (PRoctor's own tests)

PRoctor is meta-tooling. If it ships with bugs, every consumer suffers. Therefore:

**Unit layer.** `tests/fixtures/*.json` hold canned `ChangeMap`/`TestPlan`/etc. `tests/run-skill.sh <skill> <fixture>` invokes `claude -p` with the skill and a fixture, snapshot-compares JSON output. Each skill testable in isolation.

**Integration layer.** A dedicated GitHub fixtures repo (`proctor-fixtures`) with 8–10 pre-built PRs spanning every category and several known-broken cases. `tests/run-e2e.sh` runs `/proctor` against each in dry-run mode (`PROCTOR_DRY_RUN=1` writes report to stdout instead of posting). Snapshot the structured fields (item count, pass/fail flags, fix-PR presence), not raw markdown.

**Triggers.**

- Unit: on every PR to PRoctor itself
- Integration: nightly cron on `main` only (avoids infinite recursion if PRoctor's PRs run themselves)

**Out of scope.**

- Don't test chrome-devtools / claude-in-chrome / `gh` themselves
- No coverage metrics — line coverage is meaningless for prompt-based projects

## 12. Open Questions

(None blocking implementation. Capture here if surfaced during plan-writing.)

## 13. Decisions Log

| # | Decision | Rationale |
|---|---|---|
| D1 | Claude Code plugin (not standalone CLI) | Reuse existing MCP + skill ecosystem; minimize wheel-reinvention. |
| D2 | GitHub-only | Single platform = clean API. Multi-platform deferred until concrete need. |
| D3 | Fix PR (not direct push) | Human-in-the-loop on auto-modifications; safe rollback. |
| D4 | Default-skip approval in CI | Approval has cost; default to "show plan, run anyway"; opt-in to gating. |
| D5 | Five-skill pipeline + approval gate, JSON contracts between stages | Each stage independently testable, re-runnable, and replaceable. |
| D6 | `e2e-flow` as additive category | Mixed PRs need explicit cross-layer verification; not implicit. |
| D7 | No native mobile simulator by default | CI cost/flakiness; opt-in via `mobile_emulator: true`. |

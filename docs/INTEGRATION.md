# Integrating PRoctor into your project

PRoctor reads a PR's diff, plans tests, runs them, optionally opens a fix PR, and posts a structured report comment. This guide walks you from zero to having it running on your repo's PRs.

## Fastest path: the init wizard

If you have Claude Code installed and the PRoctor plugin loaded, just run this in the repo you want to add PRoctor to:

```bash
claude plugin add /path/to/proctor/plugins/proctor   # one-time
cd /your/repo
claude /proctor-init
```

The wizard detects your stack (Node/Vite/Next.js, Go, Python, Rust, …), asks 5 short questions (which stack, setup commands, auto-fix on/off, run on every push vs. require approval, auth method), then writes `.pr-test.yml` + `.github/workflows/proctor.yml`, walks you through setting the auth secret, and offers to flip the Actions PR-creation setting via API. Total time: about 2 minutes.

If the wizard asks you to set a secret, the wizard never sees the value — it tells you the exact `gh secret set` command to paste into your terminal.

The rest of this document is the manual path, plus reference for everything the wizard sets up.

## Decide which form factor

| Form | When |
|---|---|
| **Local CLI** | Solo dev or pre-PR self-review on your laptop. No GitHub Actions involvement. |
| **GitHub Action** | Team flow — PRoctor runs automatically on every PR, posts comments. |
| **Both** | Most teams. Use the local CLI for your own pre-flight; CI runs on every PR. |

Both share the same plugin, skills, and configuration file. Pick the one that fits today; the other is one-line away.

## Path A — Local CLI

### 1. Install the plugin

```bash
git clone https://github.com/zealllot/proctor /path/to/proctor
claude plugin add /path/to/proctor/plugins/proctor
```

### 2. Add `.pr-test.yml` to your repo

Drop this at the repo root and edit for your stack. See `examples/.pr-test.yml` in this repo for an annotated reference.

```yaml
setup:
  - "pnpm install --frozen-lockfile"
  - "pnpm dev > /tmp/dev.log 2>&1 &"
  - "for i in $(seq 1 60); do curl -fsS http://127.0.0.1:5173 >/dev/null && break; sleep 1; done"
base_url: "http://127.0.0.1:5173"
test_focus: ["frontend", "api"]
auto_fix: true
per_test_timeout_seconds: 60
```

### 3. Run

```bash
cd /your/repo
gh auth status   # one-time: `gh auth login` if needed
claude /proctor 123                                # PR number
claude /proctor https://github.com/org/repo/pull/123
```

PRoctor will print the test plan, ask you to approve (uncheck items you don't want), then execute.

## Path B — GitHub Action

### 1. Authentication

You need ONE of:

- **Anthropic API key** from <https://console.anthropic.com>, stored as repo secret `ANTHROPIC_API_KEY`.
- **Claude.ai OAuth token** — generate locally with `claude setup-token`, store as repo secret `CLAUDE_CODE_OAUTH_TOKEN`. Counts against your Claude.ai subscription quota; no separate billing.

```bash
# OAuth path — runs in your terminal:
claude setup-token
# copy the printed token, then:
pbpaste | gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <owner>/<repo>
echo -n | pbcopy   # clear clipboard
```

### 2. Add `.pr-test.yml`

Same file as Path A — see above.

### 3. Add the workflow

Create `.github/workflows/proctor.yml`:

```yaml
name: PRoctor

on:
  pull_request:
    types: [opened, synchronize]
  issue_comment:
    types: [created]

jobs:
  proctor:
    if: github.event_name != 'issue_comment' || contains(github.event.comment.body, '/proctor run')
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    steps:
      - uses: zealllot/proctor/github-action@v0.2.0
        with:
          # use exactly ONE of these:
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          # anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

That's it. Open a PR; PRoctor will analyze it, run tests, and post a comment.

### 4. Repository settings (required for auto-fix PRs)

If you want PRoctor's `auto_fix` flow to actually open fix PRs, enable these in your repo's **Settings → Actions → General → Workflow permissions**:

- ✅ Read and write permissions
- ✅ Allow GitHub Actions to create and approve pull requests

Without these, the executor reaches the report stage successfully but the fix-PR creation fails with `GitHub Actions is not permitted to create or approve pull requests`. The report comment will note the fix branch was pushed and ask you to open the PR manually.

You can flip both via the API in one call:

```bash
gh api -X PUT "/repos/<owner>/<repo>/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=true
```

### 5. Approval mode (optional)

Set `require_approval: true` in `.pr-test.yml`. PRoctor will post the test plan as a comment and exit; reply with `/proctor run` from any account with write access to resume execution.

## What PRoctor needs to actually run tests

PRoctor's planner picks the cheapest tool that can verify each change:

1. **`lint-only`** — source-level facts (added attribute, renamed identifier, valid YAML/JSON). No setup needed.
2. **`bash` running your existing tests** — when your repo has `vitest` / `pytest` / `go test` and the diff is in covered code. PRoctor will run your suite.
3. **`bash` with `curl`** — API contract verification. **Requires** `setup:` to start a server.
4. **`chrome-devtools`** — UI behavior, real interactions, visual regression. **Requires** `setup:` to bring up the UI.

If `setup:` is empty and a behavior needs runtime, PRoctor downgrades to grep checks against the source and flags `risk: high` so you know an environment was missing — it never silently no-ops.

## Tuning `setup:` for common stacks

The end-to-end pattern is always: install deps, start servers in the background, wait for them to respond.

### Node + Vite

```yaml
setup:
  - "corepack enable && corepack prepare pnpm@9 --activate"
  - "pnpm install --frozen-lockfile"
  - "pnpm dev > /tmp/dev.log 2>&1 &"
  - "for i in $(seq 1 60); do curl -fsS http://127.0.0.1:5173 >/dev/null && break; sleep 1; done"
base_url: "http://127.0.0.1:5173"
```

### Python + uvicorn

```yaml
setup:
  - "python -m pip install -r requirements.txt"
  - "uvicorn app:app --host 127.0.0.1 --port 8000 > /tmp/api.log 2>&1 &"
  - "for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/healthz >/dev/null && break; sleep 1; done"
base_url: "http://127.0.0.1:8000"
```

### Go + binary

```yaml
setup:
  - "go build -o /tmp/server ./cmd/server"
  - "/tmp/server > /tmp/server.log 2>&1 &"
  - "for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8080/healthz >/dev/null && break; sleep 1; done"
base_url: "http://127.0.0.1:8080"
```

### Multi-process (frontend + API)

```yaml
setup:
  - "pnpm install --frozen-lockfile"
  - "pnpm dev > /tmp/dev.log 2>&1 &"
  - "uvicorn app:app --port 8000 > /tmp/api.log 2>&1 &"
  - "for i in $(seq 1 60); do curl -fsS http://127.0.0.1:5173 >/dev/null && curl -fsS http://127.0.0.1:8000/healthz >/dev/null && break; sleep 1; done"
base_url: "http://127.0.0.1:5173"
```

The wait loop is critical: if PRoctor starts dispatching test items before the server responds, browser items will fail with connection errors instead of recoverable skips.

## Speeding up CI runs

Each Action run pays for: Claude Code install (~10s), tool installs, and your repo's `setup:` deps. PRoctor itself caches the Claude Code binary across runs (you don't have to do anything for that). For your toolchain, add the standard caching steps before the PRoctor action — the official setup-* actions all cache by default.

```yaml
jobs:
  proctor:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    steps:
      # Caches Go module + build cache automatically
      - uses: actions/setup-go@v5
        with:
          go-version: "1.22"

      # Caches the pnpm content-addressable store automatically
      - uses: pnpm/action-setup@v3
        with:
          version: 9

      # Caches pip wheels automatically (cache: 'pip' enables it)
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - uses: zealllot/proctor/github-action@v0.2.0
        with:
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Typical impact on a warm cache: 1–2 min off the run time, or about 30% on small PRs.

If your repo uses a tool not covered above (Java/Maven, Rust/Cargo, etc.), use `actions/cache@v4` directly with the standard cache paths:

| Tool | Cache path | Cache key |
|---|---|---|
| Cargo | `~/.cargo/registry`, `~/.cargo/git`, `target/` | `${{ hashFiles('**/Cargo.lock') }}` |
| Maven | `~/.m2/repository` | `${{ hashFiles('**/pom.xml') }}` |
| Gradle | `~/.gradle/caches` | `${{ hashFiles('**/*.gradle*') }}` |
| Bun | `~/.bun/install/cache` | `${{ hashFiles('**/bun.lockb') }}` |

## Troubleshooting

**Workflow fails immediately with auth error.** You set the secret but PRoctor reports `Not logged in`. Likely cause: `gh secret set` was run interactively without input, leaving the value empty. Re-set with stdin: `pbpaste | gh secret set ... -R owner/repo`.

**All test items show `status: skipped, reason: no-server`.** Either `setup:` is empty, or the server didn't start in time. Check the Action artifact (`proctor-run-<num>`) — `dev.log` has the server output.

**Pipeline reaches Stage 1 then exits silently.** Should not happen on v0.1.4+. If you see this, check the run log for `PROCTOR_PIPELINE_FAILED` markers.

**Fix PR has merge conflicts on retry.** PRoctor uses a deterministic branch name `fix-{PR#}-{shortsha}`. If a previous attempt's branch exists, the next attempt creates `fix-{PR#}-{shortsha}-2` and notes "Supersedes" in the body.

**Hitting GitHub API rate limits.** PRoctor wraps `gh` calls in `_gh_with_retry` (up to 3 attempts with exponential backoff). If you're hitting the secondary limit at workflow scale, slow your trigger rate or increase your token's quota.

**`/proctor run` comment doesn't resume.** Check the commenter has write access — PRoctor's GitHub Action gates on this explicitly.

## Versioning

Pin the action to a tag, not `@main`:

- `v0.2.0` — current (parallel execute, cost in report, anti-loop, retention, screenshot_focus, planner stub detection)
- `v0.1.18` — per-item execute dispatch (no more context-bloat bailout)
- `v0.1.17` — per-stage retry for transient claude failures
- `v0.1.16` — git clone + force-push for screenshots branch
- `v0.1.15` — fix nested-quote bash syntax error in stage 5
- `v0.1.14` — inline screenshots via dedicated branch
- `v0.1.13` — cache Claude Code install
- `v0.1.12` — rich report (per-item evidence + Action/artifact links)
- `v0.1.11` — planner uses PR body context (Slack/Jira/requirement links)
- `v0.1.10` — tolerant fix stage (fixer error doesn't kill the report)
- `v0.1.9` — schema relaxation for headless logs_ref
- `v0.1.8` — stage-by-stage bash orchestration
- `v0.1` — track latest 0.1.x patch (we don't move this tag, but you can `gh release` track via dependabot)

Breaking changes will bump to `v0.2.0`; we'll document migration in the release notes.

## Spec + plan

For deeper architecture context:

- [`docs/superpowers/specs/2026-05-09-proctor-design.md`](superpowers/specs/2026-05-09-proctor-design.md)
- [`docs/superpowers/plans/2026-05-09-proctor.md`](superpowers/plans/2026-05-09-proctor.md)

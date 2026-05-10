---
description: Interactive setup wizard. Detects your stack, asks 5–6 questions, generates `.pr-test.yml` + GitHub workflow, optionally configures the auth secret and repo permissions. Run in the repo you want to add PRoctor to.
argument-hint: ""
allowed-tools: Bash(gh *), Bash(git *), Bash(claude *), Bash(jq *), Bash(yq *), Bash(test *), Bash(ls *), Bash(cat *), Bash(grep *), Bash(printf *), Bash(echo *), Bash(mkdir *), Bash(pbcopy *), Bash(pbpaste *), Read, Write, Glob, AskUserQuestion
---

# /proctor-init

Bootstrap PRoctor on the current repository.

You are the setup wizard. Lead the user through the steps below. Be concise — output one short sentence per step explaining what you're about to do, then run the corresponding tool. Don't dump long explanations between steps; the user wants the wizard to feel fast.

## 0. Pre-flight

Run these checks first. If any fail, tell the user how to fix and stop.

```bash
git rev-parse --is-inside-work-tree    # must be true; otherwise tell user to cd into a git repo
gh auth status >/dev/null 2>&1          # must succeed; otherwise tell user to run: gh auth login
gh repo view --json nameWithOwner --jq '.nameWithOwner'   # capture as REPO_FULL_NAME
```

If `.pr-test.yml` or `.github/workflows/proctor.yml` already exists, ask whether to OVERWRITE. If user says no, stop.

## 1. Detect stack

Inspect the repo to suggest sensible defaults. Don't be exhaustive — pick the dominant stack(s):

```bash
test -f package.json   # → frontend node
test -f go.mod         # → go backend (modules)
test -f pyproject.toml || test -f requirements.txt   # → python
test -f Cargo.toml     # → rust
test -f Gemfile        # → ruby
test -f composer.json  # → php
```

For Node, also check `package.json` for hints:
- `dependencies.vite` → Vite (port 5173)
- `dependencies.next` → Next.js (port 3000)
- `dependencies.react-scripts` → CRA (port 3000)

**Server port detection** — if Q1 includes a backend stack (Go/Python/Ruby/etc.), try to derive the port from code before falling back. In rough order of reliability:

```bash
# Go: look for Listen / ListenAndServe calls with a literal port
grep -rEoh ':[0-9]{4,5}"' --include='*.go' . 2>/dev/null | head -3
# Python: app.run(port=...)  / uvicorn.run(... port=...)
grep -rEh 'port\s*=\s*[0-9]{4,5}' --include='*.py' . 2>/dev/null | head -3
# Ruby (Rails): config/puma.rb → port ENV.fetch("PORT", 3000)
# Node + custom: package.json "scripts.start" or "scripts.dev" string for ":<port>"
```

If exactly one port is found, use it. If multiple distinct ports show up, **ask the user** (Q2.6) — don't guess. If none found, assume the framework default (Vite 5173, Next 3000, Rails 3000); for plain Go/Python with no signal, **ask the user**.

Capture as `APP_PORT`.

**GOPATH-era Go detection** — if `go.mod` is absent but the repo contains Go source, it's a pre-modules project that expects to live at `$GOPATH/src/github.com/<owner>/<repo>`:

```bash
# Trigger only when go.mod is missing
if ! test -f go.mod; then
  # Any .go file in the top 3 levels (excluding vendor) → GOPATH-era Go
  find . -maxdepth 3 -name '*.go' -not -path './vendor/*' -print -quit
fi
```

If detected, capture `IMPORT_PATH=github.com/<owner>/<repo>` (split `REPO_FULL_NAME` on `/`) — Q2's setup defaults will need it for the symlink dance. Flag this in the stack summary as `"Go (GOPATH-era, no go.mod)"`.

**Resolve the action version pin** — capture `CURRENT_TAG` from the live PRoctor repo, not from any literal in this markdown:

```bash
CURRENT_TAG=$(gh release view --repo zealllot/proctor --json tagName --jq '.tagName' 2>/dev/null \
  || gh api repos/zealllot/proctor/tags --jq '.[0].name' 2>/dev/null \
  || echo "main")
```

Use this for the `uses: zealllot/proctor/github-action@<CURRENT_TAG>` line. The `main` fallback is intentional — if both API calls fail, the consumer's first run still works.

**Postgres detection** — three signals, any one is enough:

```bash
# Signal 1 (strongest): docker-compose has a postgres service
COMPOSE_HIT=$(grep -lE '^\s*image:\s*(postgres|pgvector)' docker-compose.y*ml 2>/dev/null | head -1)

# Signal 2: connection-string / DSN markers in config
CONFIG_HIT=$(grep -rlE 'sslmode=disable|postgres://|"postgres"' \
  config/ database.yml .env.example 2>/dev/null | head -1)

# Signal 3: code imports a postgres driver (catches GOPATH-era Go too)
CODE_HIT=$(grep -rlE 'gorm/dialects/postgres|lib/pq|jackc/pgx|psycopg2|psycopg|node-postgres|pg' \
  --include='*.go' --include='*.py' --include='*.js' --include='*.ts' . 2>/dev/null | head -1)
```

Capture:
- `DB_NEEDED=true` if any signal hit
- `DB_ROUTE=A` if `COMPOSE_HIT`, else `B`
- `ENV_PREFIX` — for Go projects using `configor`, grep for `ENVPrefix:\s*"([A-Z]+)"` in `config/*.go` and capture the value (e.g. `DB`); else default to `DB`. The workflow's `env:` block will use `${ENV_PREFIX}_HOST`, `${ENV_PREFIX}_PORT`, etc., so they map onto the consumer's existing config loader without code changes.
- `SCHEMA_FILE` — first hit of `db/schema.sql`, `schema.sql`, `db/structure.sql` (else empty; means user has to add their own init step)
- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME`:
  - **Route A** — parse `COMPOSE_HIT` with `yq`. Find the first service whose `image` matches `^(postgres|pgvector)`:
    ```bash
    SVC=$(yq '.services | to_entries | .[] | select(.value.image | test("^(postgres|pgvector)")) | .key' "$COMPOSE_HIT" | head -1)
    DB_PORT=$(yq ".services.$SVC.ports[0]" "$COMPOSE_HIT" | cut -d: -f1 | tr -d '"')   # host side of "9722:5432"
    DB_USER=$(yq ".services.$SVC.environment[]" "$COMPOSE_HIT" 2>/dev/null | grep -oE '^POSTGRES_USER=.*' | cut -d= -f2 | tr -d '"' || echo postgres)
    DB_PASSWORD=$(yq ".services.$SVC.environment[]" "$COMPOSE_HIT" 2>/dev/null | grep -oE '^POSTGRES_PASSWORD=.*' | cut -d= -f2 | tr -d '"' || echo postgres)
    DB_NAME=$(yq ".services.$SVC.environment[]" "$COMPOSE_HIT" 2>/dev/null | grep -oE '^POSTGRES_DB=.*' | cut -d= -f2 | tr -d '"' || echo "$REPO_NAME")
    DB_HOST=localhost
    ```
    Compose env can also be a map (`environment: { POSTGRES_USER: x }`) — handle both shapes; if the array form misses, retry with `yq ".services.$SVC.environment.POSTGRES_USER"`.
  - **Route B** — defaults: `DB_HOST=localhost`, `DB_PORT=5432`, `DB_USER=postgres`, `DB_PASSWORD=postgres`, `DB_NAME=<repo name>`.

Why parsing compose matters: stock 5432/postgres/postgres defaults will silently fail when the user's compose uses non-default ports or credentials (e.g. qor_demo maps to host 9722 with user `qor_demo`).

Append `"+ Postgres"` to the stack summary if `DB_NEEDED`.

Build a one-line stack summary like `"Node+Vite + Go (backend) + Postgres"` and proceed.

## 2. Ask the 5 questions

Use AskUserQuestion ONE question at a time. Pre-fill defaults from detection.

### Q1 — Stack confirmation

> "Detected stack: <summary>. Use this for setup defaults?"
- Yes (Recommended) — use auto-detected defaults
- No, customize — user will type setup commands manually

### Q2 — Server setup commands (only if Q1 = Yes; pre-fill per stack)

> "I'll add these `setup:` commands to `.pr-test.yml`. OK?"
Show the proposed list as the option label preview. For example for Node+Vite:
```
- corepack enable && corepack prepare pnpm@9 --activate
- pnpm install --frozen-lockfile
- pnpm dev > /tmp/dev.log 2>&1 &
- (wait loop on http://127.0.0.1:5173)
```

For **GOPATH-era Go** (no `go.mod`) the defaults need to symlink the repo into `$GOPATH/src/<IMPORT_PATH>` because the toolchain resolves imports from there:
```
- mkdir -p "$HOME/go/src/<IMPORT_PATH%/*>" && ln -sfn "$PWD" "$HOME/go/src/<IMPORT_PATH>"
- cd "$HOME/go/src/<IMPORT_PATH>" && go get -d -v ./... || true
- cd "$HOME/go/src/<IMPORT_PATH>" && go run main.go > /tmp/server.log 2>&1 &
- (wait loop on http://127.0.0.1:7000)
```
Substitute `<IMPORT_PATH>` (and `<IMPORT_PATH%/*>` = parent dir) before showing. Mention in the question that the symlink is required for pre-modules Go and let them edit the entry-point command if `main.go` lives elsewhere.

Options:
- Use these defaults (Recommended)
- Edit before saving — user provides their own list

If user picks "Edit", ask a follow-up open-ended question for the commands.

### Q2.6 — App port (skip if `APP_PORT` is already known from detection)

Only fires when Step 1's port detection found nothing or found multiple. Open-ended question:

> "Couldn't auto-detect the server port. What port does your app listen on?"

Validate input is `[0-9]{2,5}` and persist as `APP_PORT`. The wait loop and `base_url` use it.

### Q2.5 — Provision Postgres in CI (skip entirely if `DB_NEEDED=false`)

> "Detected Postgres dependency in `<CONFIG_HIT or CODE_HIT>`. Provision a database in CI?"

Branch on `DB_ROUTE`:

- **Route A** (compose file present) — Recommended option:
  - **Use existing `docker-compose.yml`** — `setup:` will run `docker compose up -d postgres`. Pros: matches local dev, picks up custom init scripts.
  - GitHub Actions services block — overrides compose with a stock Postgres 15 container.
  - No, I'll handle it myself — wizard adds nothing DB-related.

- **Route B** (no compose, only config/code hits) — Recommended option:
  - **GitHub Actions services block** — workflow gets a `services: postgres:` container. Pros: ~5–10s faster startup, no extra files.
  - I'll add my own `docker-compose.yml` later — wizard adds nothing DB-related; print a TODO line.

Persist the choice as `DB_PROVISION` ∈ `{compose, services, none}`.

### Q3 — Auto-fix behavior

> "When tests fail, should PRoctor open a fix PR with an AI-generated patch?"
- Yes, open fix PR (Recommended) — `auto_fix: true`
- No, just report failures — `auto_fix: false`

### Q4 — When to run

> "When should the workflow run?"
- On every PR push, no approval needed (Recommended) — `require_approval: false`
- Only when a maintainer comments `/proctor run` — `require_approval: true`

### Q5 — Auth method

> "How should the workflow authenticate to Claude?"
- Claude.ai subscription (OAuth token via `claude setup-token`) — Recommended for individuals/small teams
- Anthropic API key — for orgs that already have one

Don't ask for the secret value here — we'll set it interactively after files land.

## 3. Generate files

Write `.pr-test.yml` at the repo root:

```yaml
# Generated by /proctor-init. Tweak as needed.
setup:
  # Prepend ONE of these blocks based on DB_PROVISION (skip if 'none'):
  #
  # DB_PROVISION=compose (use the SERVICE NAME the compose file uses,
  # which may be 'postgres', 'db', 'pg', etc. — captured as $SVC during
  # detection. Wait by netcat-ing the host-side port instead of `docker
  # compose exec` because exec depends on the container being healthy
  # which the wait loop is supposed to verify in the first place):
  #   - docker compose up -d <SVC>
  #   - for i in $(seq 1 30); do nc -z localhost <DB_PORT> && break; sleep 1; done
  #
  # DB_PROVISION=services:
  #   (nothing here — the workflow's services: block already provisioned it.
  #    Just trust health-checks; the runner blocks job start until pg_isready passes.)
  #
  # If SCHEMA_FILE is non-empty, append after either of the above:
  #   - PGPASSWORD=postgres psql -h localhost -U postgres -d <DB_NAME> -f <SCHEMA_FILE>
  #
  # Then the stack-specific commands from Q2 (server start, wait loop).
  - <commands from Q2 and DB blocks above>
base_url: "http://127.0.0.1:<APP_PORT>"
test_focus: ["frontend", "api"]   # adjust to match the answer from Q1
require_approval: <Q4>
auto_fix: <Q3>
fix_pr_target_branch: "${PR_BRANCH}"
per_test_timeout_seconds: 60
mobile_emulator: false
```

Write `.github/workflows/proctor.yml`:

```yaml
name: PRoctor

on:
  pull_request:
    types: [opened, synchronize]
  issue_comment:
    types: [created]

jobs:
  proctor:
    if: >-
      github.event_name != 'issue_comment'
      || contains(github.event.comment.body, '/proctor run')
      || contains(github.event.comment.body, '/proctor rerun')
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    # Add this whole `services:` block ONLY if DB_PROVISION=services:
    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: <DB_NAME>
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    # Add this `env:` block if DB_PROVISION ∈ {compose, services}:
    # Values come from compose parsing (route A) or stock defaults (route B).
    # Keys use ${ENV_PREFIX} captured during detection, so they map onto the
    # consumer's existing config loader without code changes.
    env:
      <ENV_PREFIX>_HOST: <DB_HOST>
      <ENV_PREFIX>_PORT: "<DB_PORT>"
      <ENV_PREFIX>_USER: <DB_USER>
      <ENV_PREFIX>_PASSWORD: <DB_PASSWORD>
      <ENV_PREFIX>_NAME: <DB_NAME>
    steps:
      # Toolchain caching — adjust for your stack. setup-go and
      # pnpm/action-setup come with caching out of the box.
      <add setup-go@v5 if Q1 includes Go>
      <add pnpm/action-setup@v3 if Q1 includes Node>
      <add setup-python@v5 (cache: pip) if Q1 includes Python>

      - uses: zealllot/proctor/github-action@<CURRENT_TAG>
        with:
          # IMPORTANT: action inputs use HYPHENS, not underscores.
          # OAuth → `claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}`
          # API key → `anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}`
          <auth input from Q5>
```

**DB block notes for the model emitting these files:**
- `DB_PROVISION=none` → don't write the `services:` block, don't write the `env:` block, don't add DB-related setup commands. Add a TODO line in the final summary reminding the user to provision DB themselves.
- `DB_PROVISION=compose` → no `services:` block (compose handles it), but DO write the `env:` block so the app code points at `localhost:5432`.
- `DB_PROVISION=services` → both `services:` and `env:` blocks.
- If `SCHEMA_FILE` is empty, do NOT invent a schema-load step. Add a TODO line reminding the user that an empty DB is provisioned but they need their own init step.

## 4. Auth secret

Determine the secret name from Q5:
- OAuth → `CLAUDE_CODE_OAUTH_TOKEN`
- API key → `ANTHROPIC_API_KEY`

Ask: "Set the secret on `<REPO_FULL_NAME>` now?"
- Yes — guide them through it
- No, I'll do it later — print the exact command to run later and skip

If yes:
- For OAuth: instruct them to run `claude setup-token` (you can't run it for them — it opens a browser flow). Tell them to copy the printed token, then run:
  ```
  pbpaste | gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <REPO_FULL_NAME>
  echo -n | pbcopy
  ```
- For API key: instruct them to grab one from https://console.anthropic.com, then:
  ```
  read -rsp 'Paste API key: ' KEY && echo && gh secret set ANTHROPIC_API_KEY -R <REPO_FULL_NAME> --body "$KEY" && unset KEY
  ```

In both cases, the user runs the command themselves — never ask them to paste the secret to you.

## 5. Repo permissions for auto-fix

If Q3 was Yes (auto_fix), the repo's Actions permissions need to allow PR creation. Ask:

> "Auto-fix needs the repo to allow Actions to create PRs. Flip that setting now?"
- Yes — run `gh api -X PUT "/repos/<REPO_FULL_NAME>/actions/permissions/workflow" -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true`
- No — print the same command for them to run later, mention this can be done in the GitHub web UI under Settings → Actions → General

## 6. Summary + next steps

Print a short summary like:

```
✓ .pr-test.yml          (Vite + Go, dev servers on :5173 / :7000)
✓ .github/workflows/proctor.yml  (pinned to v0.2.0)
✓ Auth secret set: CLAUDE_CODE_OAUTH_TOKEN
✓ Actions PR-creation: enabled

Next:
  1. git add .pr-test.yml .github/workflows/proctor.yml
  2. git commit -m "ci: add PRoctor"
  3. Open a small PR — PRoctor will analyze it within ~10 minutes and post a report comment.

Docs:  https://github.com/zealllot/proctor/blob/main/docs/INTEGRATION.md
Stuck? Skim the Troubleshooting section there before re-running.
```

If any step was skipped (auth not set, perms not flipped), call those out as "TODO" lines so the user remembers.

## Style guide

- Don't ask questions you can already answer (skip Q5 if `CLAUDE_CODE_OAUTH_TOKEN` is already set on the repo; skip Q2.5 if `DB_NEEDED=false`).
- Don't paste large config blocks unless asked — show the diff or path, not the body.
- If anything errors mid-flow, report the failure plainly and let the user re-run.
- Never proceed past pre-flight if `gh` isn't authenticated.

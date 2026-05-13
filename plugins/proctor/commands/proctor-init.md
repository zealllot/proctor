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

Also resolve **once** the current PRoctor tag (used everywhere we pin `zealllot/proctor/github-action@<TAG>`):

```bash
CURRENT_TAG=$(gh release view --repo zealllot/proctor --json tagName --jq '.tagName' 2>/dev/null \
  || gh api repos/zealllot/proctor/tags --jq '.[0].name' 2>/dev/null \
  || echo "main")
```

## 0.5. Existing config detection

Before anything else, check what PRoctor state is already in this repo:

```bash
EXISTING_PR_TEST_YML=$(test -f .pr-test.yml && echo yes)
EXISTING_WORKFLOW=$(test -f .github/workflows/proctor.yml && echo yes)
# Detect current pin (if any) — used to classify "v0.2 era" vs "v0.3 era"
CURRENT_PIN=$(grep -oE 'zealllot/proctor/github-action@v[0-9]+\.[0-9]+\.[0-9]+' \
                .github/workflows/proctor.yml 2>/dev/null | head -1 | sed 's|.*@||')
HAS_AUTH_BLOCK=$(grep -qE '^auth:' .pr-test.yml 2>/dev/null && echo yes)
```

Branch on what's there:

- **Neither file exists** → fresh install. Set `MODE=fresh`. Continue to Step 1.
- **Workflow exists but action pin is current and `auth:` block already present** → already on v0.3. Tell the user "PRoctor is already integrated and up to date" and stop unless they want to re-run for some other reason.
- **Files exist but no `auth:` block** (typical v0.2.x consumer) → set `MODE=migrate`. Ask:

  > "Detected existing PRoctor integration pinned at <CURRENT_PIN>. v0.3.0 introduces:
  > - Auth + multi-account testing against an already-running server (no CI bring-up needed for runtime tests).
  > - Per-developer `.pr-test.local.yml` overrides.
  >
  > How would you like to proceed?"
  > - **Migrate to v0.3 existing-env mode (Recommended)** — keep `require_approval` / `auto_fix` / etc., drop `setup:`, add `auth:` block. Bump workflow pin to `<CURRENT_TAG>`, add secret pass-through.
  > - **Keep v0.2 setup-based config, just bump the version pin** — minimal change. Skip everything else.
  > - **Start fresh** — discard existing config and re-run the wizard from scratch.

- **Files exist with `auth:` block already, but pin is older than `<CURRENT_TAG>`** → `MODE=bump-only`. Patch the workflow to the latest pin and stop.

`MODE` decides what runs next:

| MODE | Skip Step 1–6 | Run Section 7 | Apply via |
|---|---|---|---|
| `fresh` | — | — | Step 1 onward (full flow, asks Q0 for path) |
| `migrate` | yes | yes (skip Q-EnvC count if existing config already lists accounts) | Section 7 + Section 8 patcher |
| `bump-only` | yes | — | Section 8 patcher (workflow version only) |

For `migrate` and `bump-only`, **do NOT call out to Sections 1–6**; they're CI-bring-up flow only.

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

## 2. Ask the questions

Use AskUserQuestion ONE question at a time. Pre-fill defaults from detection.

### Q0 — Test strategy (CRITICAL — branches the rest of the wizard)

> "Where will PRoctor run its tests against?"
- **Existing running server** (Recommended) — already-deployed test env or your local `docker-compose up + go run`. PRoctor logs in as an admin and drives the real app. Closer to production behavior, faster runs, no CI bring-up duplication.
- **PRoctor brings up a fresh server in CI** — for projects without a deployed test env, or where every PR needs an isolated stack. Heavier setup.

If user picks "Existing running server" → **skip Q1–Q4 below**, jump to [Section 7: Existing-env path](#7-existing-env-path-auth-config).

Otherwise continue with Q1 below.

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

⚠ For `/proctor run` and `/proctor rerun` comment triggers to work:
    The workflow file must exist on the DEFAULT BRANCH (main/master).
    GitHub reads issue_comment workflows from the default branch only —
    if proctor.yml only lives on your feature branch, comment triggers
    silently do nothing. Merge the workflow to default before relying
    on comment-driven flows.

Docs:  https://github.com/zealllot/proctor/blob/main/docs/INTEGRATION.md
Stuck? Skim the Troubleshooting section there before re-running.
```

If any step was skipped (auth not set, perms not flipped), call those out as "TODO" lines so the user remembers.

## 7. Existing-env path: auth config

Entered when Q0 = "Existing running server" (`MODE=fresh`) OR when migrating from v0.2 (`MODE=migrate`). Captures auth + accounts then hands off to Section 8 for file generation / patching.

In `MODE=migrate`, read the existing `.pr-test.yml` first and use its values as DEFAULTS so the user can press-enter to keep what they have. Specifically, parse out: `require_approval`, `auto_fix`, `base_url` (if present), and `mobile_emulator`. Treat these as locked answers — don't re-ask.

### Step 7a — Login mechanism (Q-EnvA)

Ask, one short question via AskUserQuestion:

> "How does an admin log into the app you want PRoctor to test?"
- **Form: email + password + TOTP (2FA)** (Recommended) — sets `auth.type: form_with_totp`. Continue to 7b.
- **Form: email + password, no 2FA** — also `form_with_totp` schema-wise; mark the `totp` selector + `totp_seed_env` as TODO in the generated file, document for the user that the schema requires them but the runtime can treat 2FA as no-op when the seed is empty. Continue to 7b.
- **Something else** (SSO, magic link, OAuth) — skip the auth block. Tell the user PRoctor v0.3.0 doesn't generate other auth types yet; they can run without `auth:` (legacy mode) or hand-edit the file. Stop after generating the no-auth `.pr-test.yml`.

### Step 7b — Login form selectors (Q-EnvB)

Pre-fill with qor/auth conventions (matches most ThePlant projects). Ask:

> "Confirm the login form selectors. Defaults assume qor/auth conventions — Open `<base_url>/auth/login` in DevTools and check if you need to edit."

| field | default |
|---|---|
| `login_url` | `/auth/login` |
| `email` | `input[name="login"]` |
| `password` | `input[name="password"]` |
| `totp` | `input[name="passcode"]` |
| `submit` | `button[type="submit"]` |

Options:
- **Use defaults (Recommended)** — store as-is.
- **Edit** — open follow-up free-text input for each of the 5 fields with the default pre-filled.

### Step 7c — Discover admin roles from the codebase

**Don't ask the user to count their roles — detect them.** Run a few greps for common role-enumeration patterns. Bias toward false positives (show everything found); the user will deselect what they don't care to test.

```bash
# Go: const Role_developer = "developer" / type Role string / Role_admin
grep -rhoE '\bRole[_A-Z][A-Z][a-zA-Z_]+' \
  --include='*.go' . 2>/dev/null | sort -u

# Go: enum-style with values  Role_editor = 2
grep -rhoE '\bRole_[A-Za-z]+\s*=' \
  --include='*.go' . 2>/dev/null | sed 's|\s*=$||' | sort -u

# TypeScript / JS: enum Role { Editor, Viewer } or const ROLES = [...]
grep -rhE '\b(enum\s+Role|type\s+Role\s*=|ROLES\s*=)' \
  --include='*.ts' --include='*.tsx' --include='*.js' . 2>/dev/null | head -10

# Python: class Role(Enum): ADMIN = "admin"
grep -rhE '\bclass\s+Role\b|^\s*[A-Z_]+\s*=\s*"[a-z_]+"' \
  --include='*.py' . 2>/dev/null | head -20

# Ruby: role :admin / has_role :editor
grep -rhE 'has_role\s+:|enum\s+role\s*:' \
  --include='*.rb' . 2>/dev/null | head -20

# Database / config: roles seeded in migrations or seeds
grep -rhE '"(developer|admin|editor|viewer|reader|writer|operator|manager)"\s*[,)\]]' \
  --include='*.sql' --include='*.yml' --include='*.json' . 2>/dev/null | head -20
```

Aggregate everything into `DETECTED_ROLES` (deduplicated, lowercased). Strip the `Role_` prefix when present. Examples after dedupe:
- `developer`, `editor`, `viewer`, `admin`
- (or just `admin` for a simple app, or empty if nothing matched)

Present the result via AskUserQuestion (multi-select):

> "I found these candidate admin roles in the codebase. Which ones should PRoctor test under? (Each picked role becomes one auth account in `.pr-test.yml`.)"
>
> Options: `<each DETECTED_ROLES entry>` + "Add a role not in this list" + "Just one generic admin account"

**Branching from this answer:**

- Picked ≥1 from the detected list → use those as the account names. Skip "Add a role not in this list" unless user explicitly clicked it.
- Picked "Add a role not in this list" → open a free-text follow-up: "Type the role name (will become `auth.accounts[].name`)". Loop until user types `done`.
- Picked "Just one generic admin account" → single account, name = `admin`.
- Nothing detected at all → present a different question: "What admin roles do you want to test? Type each name, one at a time. Type `done` when finished."

Save the final list as `ROLE_NAMES = [name, name, name]`.

`MODE=migrate` special case: if the existing `.pr-test.yml` already has `auth.accounts:` declared, **skip the detection grep entirely** and confirm:

> "Existing `.pr-test.yml` declares N accounts: `<names>`. Keep them, or rediscover from the codebase?"
- Keep → reuse the existing list, skip Step 7d (accounts already fully configured).
- Rediscover → fall through to the grep-driven flow above.

### Step 7d — Per-account credentials

For each entry in `ROLE_NAMES` (from Step 7c), ask ONE AskUserQuestion combining `role_label` + 3 env var names. `name:` comes from `ROLE_NAMES[i]` directly — no need to re-ask.

Pre-fill env var names by uppercasing the role name and following the `AI_TESTER_<ROLE>_<KIND>` convention:

```
For each role_name in ROLE_NAMES:
    role_upper = role_name.upper()
    pre_fill_email_env    = f"AI_TESTER_{role_upper}_EMAIL"
    pre_fill_password_env = f"AI_TESTER_{role_upper}_PASSWORD"
    pre_fill_totp_seed_env = f"AI_TESTER_{role_upper}_TOTP_SEED"
```

Examples:
- `developer` → `AI_TESTER_DEVELOPER_EMAIL` / `_PASSWORD` / `_TOTP_SEED`
- `editor`    → `AI_TESTER_EDITOR_EMAIL` / ...
- `viewer`    → `AI_TESTER_VIEWER_EMAIL` / ...
- `cms_manager` → `AI_TESTER_CMS_MANAGER_EMAIL` / ... (preserves snake_case)

Pre-fill `role_label` as a generic placeholder; the user almost always edits this:

```
role_name = "developer" → role_label suggestion: "Developer (full admin)"
                          (or just "developer" if you can't guess scope)
```

Ask:

> "Account `<role_name>`: confirm `role_label` (a one-line description of what this role can do — shows up in PRoctor's planner context) and the env var names that will hold its credentials."
>
> Pre-filled fields: role_label, email_env, password_env, totp_seed_env. User edits any if defaults don't fit.

Save each as `ACCOUNTS[i] = {name: ROLE_NAMES[i], role_label, email_env, password_env, totp_seed_env}`.

**Bulk-confirm shortcut**: if `ROLE_NAMES` has ≥3 entries, before looping ask:

> "I'll generate `AI_TESTER_<ROLE>_<KIND>` env var names for all <N> roles. Want to confirm each one individually, or accept the convention in bulk?"
- **Accept bulk (Recommended)** — skip the per-role question, fill in `role_label` as a generic `<role_name> account` and use the pre-filled env vars.
- **Confirm each** — loop with the question above per role.

### Step 7e — Base URL (Q-EnvE)

In `MODE=migrate`: if the existing `.pr-test.yml` already has `base_url`, ask to confirm (default = existing).

In `MODE=fresh`: ask via AskUserQuestion:

> "What URL is the deployed test environment at?"
Free-text input. Pre-fill examples: `https://cms.<your-app>.theplant-dev.com`. Validate:

- Must start with `https?://` (case-insensitive).
- If the URL contains any of `prod.`, `.qorcommerce.com`, `www.<consumer-real-domain>.<tld>`, REFUSE and re-ask. Hard refusal — not a warning.
- Emit a notice that the wizard cannot be 100% certain whether a URL is prod and the user is responsible for not pointing PRoctor at production.

Save as `BASE_URL`.

## 8. Apply changes (Sections 7 produces; this section commits to disk)

This section is reached at the end of `MODE=fresh` (existing-env path) OR `MODE=migrate` OR `MODE=bump-only`. It's the single file-mutation point. **Show the user a diff preview** before each Write — they can refuse any single change.

### 8a — Write `.pr-test.yml`

For `MODE=fresh` or `MODE=migrate`: generate the YAML from Section 7's captured answers. Template:

```yaml
# Managed by /proctor-init. Re-run the wizard to regenerate.
# Per-developer overrides go in .pr-test.local.yml (gitignored).

base_url: <BASE_URL>

auth:
  type: form_with_totp
  login_url: <selectors.login_url>
  selectors:
    email: <selectors.email>
    password: <selectors.password>
    totp: <selectors.totp>
    submit: <selectors.submit>
  accounts:
    - name: <ACCOUNTS[1].name>
      role_label: "<ACCOUNTS[1].role_label>"
      email_env: <ACCOUNTS[1].email_env>
      password_env: <ACCOUNTS[1].password_env>
      totp_seed_env: <ACCOUNTS[1].totp_seed_env>
    # ... repeat for ACCOUNTS[2..N]

require_approval: <migrate: from existing | fresh: from Q4>
auto_fix: <migrate: from existing | fresh: from Q3>
fix_pr_target_branch: "${PR_BRANCH}"
per_test_timeout_seconds: <migrate: from existing or 60 | fresh: 60>
mobile_emulator: <migrate: from existing or false | fresh: false>
```

For `MODE=migrate`, DROP the existing `setup:` block (no longer needed in existing-env mode) and emit a `setup_removed` note to mention in the summary. Don't silently drop other keys we don't understand — preserve them at the end of the file with a `# Preserved from previous .pr-test.yml:` comment.

### 8b — Write `.pr-test.local.yml.example`

```yaml
# Per-developer overrides. Copy this to .pr-test.local.yml and edit.
# This file is gitignored.
#
# Most devs just need to override base_url to point at their local server:
base_url: http://localhost:<APP_PORT or "PORT_HERE">

# To override credentials for a fully different local-only account set,
# replace auth.accounts wholesale (the array REPLACES the base on merge —
# partial entries would silently fall back to test env creds):
#
# auth:
#   accounts:
#     - name: developer
#       role_label: "Local dev (full admin)"
#       email_env: LOCAL_DEV_EMAIL
#       password_env: LOCAL_DEV_PASSWORD
#       totp_seed_env: LOCAL_DEV_TOTP_SEED
```

### 8c — Update `.gitignore`

Read `.gitignore`. For each of these lines, append it ONLY if not already present:

```
.pr-test.local.yml
.proctor/
```

### 8d — Patch `.github/workflows/proctor.yml` (DO NOT overwrite)

This is the **most delicate** step. Different MODES touch this file differently:

**`MODE=fresh` (existing-env path)**: there is no existing workflow file — generate the full skeleton from Section 3's template, but using the existing-env auth env pass-through (see below). Action pin = `<CURRENT_TAG>`.

**`MODE=migrate`**: existing workflow file is present and was hand-written or generated by an older wizard. **Patch in place** — don't regenerate. Two surgical edits:

1. **Bump the action pin**: find the line matching `^\s*-\s*uses:\s*zealllot/proctor/github-action@v[0-9]+\.[0-9]+\.[0-9]+` and replace the version with `<CURRENT_TAG>`. If there are multiple matches, edit all of them.

2. **Insert `env:` pass-through block**: find the same `uses:` line. If the next non-blank YAML key at the same indentation is already `env:`, scan its body for missing entries; insert only the ones not present. If the next key is `with:` (not `env:`), insert a fresh `env:` block immediately before it at the same indentation, listing every account's three env vars:

   ```yaml
         env:
           AI_TESTER_DEV_EMAIL: ${{ secrets.AI_TESTER_DEV_EMAIL }}
           AI_TESTER_DEV_PASSWORD: ${{ secrets.AI_TESTER_DEV_PASSWORD }}
           AI_TESTER_DEV_TOTP_SEED: ${{ secrets.AI_TESTER_DEV_TOTP_SEED }}
           # ... one triple per ACCOUNTS[i]
   ```

   The set of env var names comes from `ACCOUNTS[*].{email_env, password_env, totp_seed_env}` — emit them in the order accounts were declared so the list is stable across re-runs.

Do NOT touch: `name:`, `on:`, `concurrency:`, `permissions:`, `if:`, any other `runs-on`, `services:` block (if present), or any user-added step.

**`MODE=bump-only`**: do only step 1 (bump action pin), nothing else.

### 8e — Auth secrets walkthrough

For every distinct env var name across `ACCOUNTS[*]`, do NOT ask the user for the value. Print the command they should run, in this exact format (preserves the value-never-touches-Claude property):

```
For account "developer", set its 3 secrets:

  read -rsp "Paste developer email: " V && echo \
    && gh secret set AI_TESTER_DEV_EMAIL -R <REPO_FULL_NAME> --body "$V" && unset V

  read -rsp "Paste developer password: " V && echo \
    && gh secret set AI_TESTER_DEV_PASSWORD -R <REPO_FULL_NAME> --body "$V" && unset V

  read -rsp "Paste developer TOTP base32 seed: " V && echo \
    && gh secret set AI_TESTER_DEV_TOTP_SEED -R <REPO_FULL_NAME> --body "$V" && unset V

⚠ TOTP seed = the long base32 string under the QR code at 2FA setup,
   NOT the 6-digit code (which expires every 30 seconds).
```

Repeat for each account.

If `<CLAUDE_CODE_OAUTH_TOKEN>` isn't already set as a repo secret, also walk through that one (same pattern, the value comes from `claude setup-token`).

### 8f — Default-branch caveat reminder

Print, regardless of mode:

```
⚠ For /proctor run and /proctor rerun comment triggers to fire:
  proctor.yml MUST exist on the DEFAULT BRANCH (main/master).
  GitHub reads issue_comment workflows from the default branch only.

  If this is a fresh install: merge this PR to master before any
  /proctor run comments will be received.
```

### 8g — Summary

```
Done. Files changed:
  ✓ .pr-test.yml                           (created / updated)
  ✓ .pr-test.local.yml.example             (created — copy to .pr-test.local.yml)
  ✓ .gitignore                             (.proctor/ and .pr-test.local.yml ignored)
  ✓ .github/workflows/proctor.yml          (action pinned to <CURRENT_TAG>, secrets pass-through added)

Next steps:
  1. Commit the above and open a PR (or merge if you've batched it on master).
  2. Run the secrets-set commands above for each account.
  3. (If migration) The old setup: block was dropped — see PRESERVED comment block
     at the bottom of .pr-test.yml for anything else that wasn't recognized.
  4. Test it: `/proctor run` on any PR.
```

If any step was skipped (auth not generated, perms not flipped, secrets not set), call those out as "TODO" lines so the user remembers.

## Style guide

- Don't ask questions you can already answer (skip Q5 if `CLAUDE_CODE_OAUTH_TOKEN` is already set on the repo; skip Q2.5 if `DB_NEEDED=false`).
- Don't paste large config blocks unless asked — show the diff or path, not the body.
- If anything errors mid-flow, report the failure plainly and let the user re-run.
- Never proceed past pre-flight if `gh` isn't authenticated.

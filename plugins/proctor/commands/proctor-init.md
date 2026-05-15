---
description: Interactive setup wizard. Detects your stack, asks 5–6 questions, generates `.proctor/config.yml` + GitHub workflow, optionally configures the auth secret and repo permissions. Run in the repo you want to add PRoctor to.
argument-hint: ""
allowed-tools: Bash(gh *), Bash(git *), Bash(claude *), Bash(jq *), Bash(yq *), Bash(test *), Bash(ls *), Bash(cat *), Bash(grep *), Bash(printf *), Bash(echo *), Bash(mkdir *), Bash(pbcopy *), Bash(pbpaste *), Read, Write, Glob, AskUserQuestion
---

# /proctor-init

Bootstrap PRoctor on the current repository.

## ⚠ CRITICAL (v0.5.0+): the wizard is a state-machine loop

**Don't follow the prose below step-by-step.** v0.5.0 moved the wizard's control flow into a Python state machine at `scripts/wizard_run.py`. Your job is a tight LOOP that drives the script — each iteration reads one envelope, surfaces the indicated AskUserQuestion / Bash / message, and re-invokes the script. The legacy prose below this section is kept ONLY as fallback documentation for modes the state machine doesn't yet implement (fresh / migrate / bump-only-with-seed).

**Stop conditions** (the only legitimate ones):
- Envelope type is `done` → emit summary, exit loop.
- Envelope type is `error` → emit error, exit loop.
- An `ask_user` envelope's AskUserQuestion is currently displayed and awaiting a user response.

**If you completed any single step and your turn ends without re-invoking `wizard_run.py`** — that's the same stall pattern the user has been hitting all session. Don't do it. Iterate.

## Wizard loop

### 0. Pre-flight (run ONCE at the start, then loop below)

```bash
# Must succeed:
git rev-parse --is-inside-work-tree >/dev/null
gh auth status >/dev/null 2>&1

# Resolve the latest PRoctor release tag (used by the state machine):
CURRENT_TAG=$(gh release view --repo zealllot/proctor --json tagName --jq '.tagName' 2>/dev/null \
  || gh api repos/zealllot/proctor/tags --jq '.[0].name' 2>/dev/null \
  || echo "main")
```

### 1. Loop body — each iteration is ONE assistant turn

For each iteration, do exactly these tool calls in order:

**1a. Invoke the state machine** (NO arguments after the first invocation unless the previous iteration's envelope was `ask_user` or `bash`):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wizard_run.py \
    --state-file .proctor/wizard-state.json \
    --current-tag "$CURRENT_TAG" \
    --repo-root . \
    --plugin-root "${CLAUDE_PLUGIN_ROOT}" \
    ${PREV_ANSWER:+--answer "$PREV_ANSWER"} \
    ${PREV_BASH_RC:+--bash-rc "$PREV_BASH_RC"}
```

The script emits one JSON envelope to stdout. Parse the `type` field.

**1b. Branch on `type`** — exactly one action per iteration:

- **`type=ask_user`**: call `AskUserQuestion` with the `header` / `question` / `options` from the envelope. If the envelope has `"multi_select": true` (v0.7.8+, used by the amend-daemons binary-picker), call AskUserQuestion in multi-select mode and join the selected labels with `", "` before passing back as `--answer`. After the user answers, save `PREV_ANSWER=<selected-label(s)>` and `PREV_BASH_RC=` (clear). **Continue to next iteration (1a)** — DO NOT exit the turn after the AskUserQuestion answer; immediately re-invoke `wizard_run.py` with `--answer "$PREV_ANSWER"`.

- **`type=show`**: emit the `markdown` field verbatim to chat. Save `PREV_ANSWER=` (clear) and `PREV_BASH_RC=`. **Continue to next iteration (1a)** in the same response — don't end the turn.

- **`type=bash`**: run the `command` field as a Bash tool call. Save the exit code as `PREV_BASH_RC=<exit>`. Save `PREV_ANSWER=`. **Continue to next iteration (1a)** in the same response.

- **`type=done`**: emit the `summary` field to chat. Exit the loop. End the turn.

- **`type=error`**: emit the `message` field to chat. Exit the loop. End the turn.

**1c. There is no other branch.** If the envelope's `type` isn't one of the five above, that's a wizard_run.py bug — emit the raw envelope as a bug report and exit.

### 2. Loop discipline (anti-stall checklist)

- After each `ask_user` answer, the script must re-run in the SAME response (with `--answer "$PREV_ANSWER"`). Don't end your turn after the AskUserQuestion answer; that's how the old "继续 prompt" stalls happened.
- After each `bash` command, the script must re-run in the same response (with `--bash-rc "$PREV_BASH_RC"`).
- After each `show`, the script must re-run in the same response (no flags).
- Only `done` / `error` end the turn. If you find yourself ending the turn for any other reason, that's a bug — keep looping.

The state file `.proctor/wizard-state.json` persists between iterations. If the AI process dies mid-flow, re-invoking the wizard resumes from the last persisted step. Safe to interrupt.

---

## Legacy prose (fallback for modes the state machine doesn't yet implement)

The sections below describe the wizard's behavior in detail. v0.5.0's state machine handles `current` / `bump-only` / `needs-local-regen` / `legacy-migration` directly. For modes `fresh` / `migrate` / `bump-only-with-seed`, the state machine emits a `show` envelope pointing you here and exits — you then walk this prose manually. v0.5.x will migrate the remaining modes into Python.

## 0. Pre-flight (legacy reference)

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
EXISTING_PR_TEST_YML=$(test -f .proctor/config.yml && echo yes)
EXISTING_WORKFLOW=$(test -f .github/workflows/proctor.yml && echo yes)
# Detect current pin (if any) — used to classify "v0.2 era" vs "v0.3 era"
CURRENT_PIN=$(grep -oE 'zealllot/proctor/github-action@v[0-9]+\.[0-9]+\.[0-9]+' \
                .github/workflows/proctor.yml 2>/dev/null | head -1 | sed 's|.*@||')
HAS_AUTH_BLOCK=$( (grep -qE '^auth:' .proctor/config.yml 2>/dev/null || grep -qE '^auth:' .pr-test.yml 2>/dev/null) && echo yes )

# v0.4.0 layout migration: detect whether this repo is on the v0.3.x
# scattered layout (.pr-test.yml at root, hack/proctor-seed-local.sh)
# vs the v0.4.0 consolidated layout (.proctor/config.yml, .proctor/seed-local.sh).
LEGACY_LAYOUT=$( (test -f .pr-test.yml || test -x hack/proctor-seed-local.sh) && echo yes )
NEW_LAYOUT=$(test -f .proctor/config.yml && echo yes)
```

Also check whether a local-seed helper exists (orthogonal to MODE — used to decide whether Step 8c-pre runs):

```bash
HAS_SEED_SCRIPT=$( \
  (test -x .proctor/seed-local.sh \
   || test -x hack/proctor-seed-local.sh \
   || test -x scripts/proctor-seed-local.sh \
   || test -x ./proctor-seed-local.sh) && echo yes \
)

# v0.4.4+: check whether the gitignored .proctor/local.yml (the
# file PRoctor actually READS at runtime — contains setup commands
# + inline credentials, generated by the seed script) is present.
# If the seed script exists but local.yml is missing, the developer
# either (a) never ran the seed script, or (b) deleted local.yml
# because it was broken and expects the wizard to detect + regenerate.
# Either way: don't silently fall through to bump-only — drive the
# regeneration path.
HAS_LOCAL_YML=$(test -f .proctor/local.yml && echo yes)
NEEDS_LOCAL_REGEN=$( \
  [ "$HAS_SEED_SCRIPT" = yes ] && [ -z "$HAS_LOCAL_YML" ] && echo yes \
)
```

**v0.4.0 layout-migration branch** (runs BEFORE the normal MODE branching):

If `LEGACY_LAYOUT=yes` AND `NEW_LAYOUT=` (empty): the consumer is on the v0.3.x scattered layout. v0.4.0 consolidated everything under `.proctor/`. Offer migration via AskUserQuestion:

> "Detected v0.3.x config layout (`.pr-test.yml`, `hack/proctor-seed-local.sh`). v0.4.0 consolidated everything under `.proctor/`. Migrate?"

Options:
- **Migrate to v0.4.0 layout (Recommended)** — `git mv` the files, update `.gitignore`. The plugin reads either layout at runtime, but the new layout is cleaner and required for future versions.
- **Keep current layout** — the v0.3.x compatibility shim in `schema.load_config` will keep reading the old paths, but a deprecation warning fires on every run.

If user picks "Migrate to v0.4.0":

**Step 1 — preview**: before touching anything, print what WILL move so the user sees it explicitly:

```bash
echo "About to migrate to v0.4.0 layout:"
[ -f .pr-test.yml ]                && echo "  git mv .pr-test.yml                → .proctor/config.yml"
[ -f .pr-test.local.yml.example ]  && echo "  git mv .pr-test.local.yml.example → .proctor/local.yml.example"
[ -f .pr-test.local.yml ]          && echo "     mv .pr-test.local.yml           → .proctor/local.yml         (gitignored — plain mv)"
[ -x hack/proctor-seed-local.sh ]  && echo "  git mv hack/proctor-seed-local.sh → .proctor/seed-local.sh"
echo "  patch .gitignore: drop old PRoctor lines, add .proctor/local.yml + .proctor/runs/"
echo
```

**Step 2 — execute** (each `git mv` guarded by `[ -f ]` so re-runs are idempotent — if a file is already in the new place from a prior migration attempt, that line is a no-op instead of an error):

```bash
mkdir -p .proctor
[ -f .pr-test.yml ]                && git mv .pr-test.yml                .proctor/config.yml
[ -f .pr-test.local.yml.example ]  && git mv .pr-test.local.yml.example  .proctor/local.yml.example
[ -f .pr-test.local.yml ]          && mv     .pr-test.local.yml          .proctor/local.yml   # gitignored — plain mv preserves the file content
[ -x hack/proctor-seed-local.sh ]  && git mv hack/proctor-seed-local.sh  .proctor/seed-local.sh

# .gitignore handling — robust to:
#   (a) .gitignore not existing yet (rare but possible — touch first)
#   (b) PRoctor lines already moved to new form (re-run case — grep-guard the appends)
#   (c) the consumer's other gitignore content (only edit OUR lines)
touch .gitignore
# Remove legacy PRoctor-specific lines (exact-match, won't touch consumer's other entries)
sed -i.bak \
    -e '/^\.pr-test\.local\.yml$/d' \
    -e '/^\.pr-test\.local\.yml\.example$/d' \
    -e '/^\.proctor\/runs\/\?$/d' \
    -e '/^hack\/proctor-seed-local\.sh$/d' \
    -e '/^# PRoctor (.*)$/d' \
    .gitignore && rm -f .gitignore.bak

# Append the canonical v0.4.0 block, but only the lines that aren't
# already present (grep -F to treat as fixed string, -q for quiet).
# v0.7.3: also covers `.proctor/wizard-state.json` (transient
# wizard state file — auto-deleted on done, but if the wizard
# crashes mid-flow the file persists for resume; either way it
# should NOT be committed).
{
    grep -qxF '# PRoctor (v0.4.0+) layout' .gitignore || echo '# PRoctor (v0.4.0+) layout'
    grep -qxF '.proctor/local.yml'         .gitignore || echo '.proctor/local.yml'
    grep -qxF '.proctor/runs/'             .gitignore || echo '.proctor/runs/'
    grep -qxF '.proctor/wizard-state.json' .gitignore || echo '.proctor/wizard-state.json'
} >> .gitignore.tmp
[ -s .gitignore.tmp ] && {
    printf '\n' >> .gitignore   # leading newline only when we're appending something
    cat .gitignore.tmp >> .gitignore
}
rm -f .gitignore.tmp
```

**Step 3 — summary**: print what actually happened so the user sees the result:

```bash
echo "Migrated:"
git status --short .proctor/ .gitignore .pr-test.yml hack/ 2>/dev/null | sed 's/^/  /'
echo
echo "Run:"
echo "  git diff .gitignore     # review the gitignore changes"
echo "  git status              # see the renames git tracked"
```

Then continue to normal MODE branching below (re-evaluate the env vars after the migration — `LEGACY_LAYOUT` flips to empty, `NEW_LAYOUT=yes`).

**v0.4.5+ deterministic decision (REQUIRED FIRST STEP)**

Before reading the prose bullets below, **run the decision script** to get an unambiguous MODE pick. The bullets are documentation of what each MODE means; the script is the source of truth for which MODE to use. The v0.3-and-earlier "AI walks bullets, picks first match" flow failed in production because the AI silently skipped bullets keyed on detection-block-computed variables. The script removes that failure mode.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wizard_decide_mode.py \
    --current-tag "$CURRENT_TAG" \
    --repo-root .
```

Output is JSON with shape `{state, mode, next_action, ask_user}`:
- `mode` is one of: `fresh`, `legacy-migration`, `needs-local-regen`, `bump-only-with-seed`, `migrate`, `bump-only`, `current`
- `ask_user` is either `null` (no user input needed; execute `next_action` directly) or an object with `{header, question, options[]}` — surface as an AskUserQuestion call.

**You MUST**:
1. Run the script.
2. Read the `mode` field. THAT is the branch — do not re-derive from the file facts below.
3. If `ask_user` is non-null, immediately call AskUserQuestion with those options; otherwise execute the `next_action` for the chosen mode.

The bullets below describe what each MODE does. Use them to look up the action — NOT to pick the mode (the script already picked).

MODE reference (script picked one of these — look up the action; do NOT re-evaluate against these conditions):

- **Neither file exists** → fresh install. Set `MODE=fresh`. Continue to Step 1.
- **`NEEDS_LOCAL_REGEN=yes`** (v0.4.4+) → seed script exists but `.proctor/local.yml` is missing. Developer either never ran the seed script, or deleted local.yml because it was broken and expects regeneration. AskUserQuestion:

  > "Detected `.proctor/seed-local.sh` exists but `.proctor/local.yml` is missing. The local config is what PRoctor reads at runtime (setup commands + credentials). How would you like to proceed?"

  Options:
  - **Regenerate seed-local.sh AND re-run it** (Recommended) — fall into the full Step 7 path (re-runs Step 7f setup-command confirmation, so any v0.4.x improvements like env-source confirmation get applied) + Step 8c-pre regenerates the seed script + suggests running it. This is the most aggressive fix; covers both "the existing seed script had wrong setup commands" and "I just want a fresh local.yml".
  - **Just run the existing seed-local.sh** — wizard prints `./.proctor/seed-local.sh` as the next-step command without touching the seed script. Faster but won't pick up v0.4.x setup-confirmation improvements baked into newer Step 7f.
  - **Skip — I'll handle .proctor/local.yml myself** — wizard does nothing extra; bump-only path continues as before.

  Branch by the answer:
  - **Regenerate seed-local.sh** → set `MODE=migrate` (re-runs Section 7 including Step 7f, and Step 8c-pre regenerates seed script). After the wizard finishes, summary explicitly tells user to run `./.proctor/seed-local.sh`.
  - **Just run the existing** → continue to the next MODE-detection branch BUT add a "run `./.proctor/seed-local.sh` now" item to the wizard's exit summary so it's not silently forgotten.
  - **Skip** → continue to next MODE-detection branch.

- **Workflow exists, pin is current, `auth:` block present, AND seed script exists, AND `HAS_LOCAL_YML=yes`** → fully set up. Tell the user "PRoctor is already integrated and up to date" and stop.
- **Workflow exists, pin is current, `auth:` block present, but seed script MISSING** → set `MODE=bump-only` (no other workflow patching) and `NEEDS_SEED_SCRIPT=yes`. The wizard ALSO runs Step 8c-pre to generate the missing seed script. Skip Sections 1–7 (config already correct).
- **Files exist but no `auth:` block** (typical v0.2.x consumer) → set `MODE=migrate`. Ask:

  > "Detected existing PRoctor integration pinned at <CURRENT_PIN>. v0.3.0 introduces:
  > - Auth + multi-account testing against an already-running server (no CI bring-up needed for runtime tests).
  > - Per-developer `.proctor/local.yml` overrides.
  >
  > How would you like to proceed?"
  > - **Migrate to v0.3 existing-env mode (Recommended)** — keep `require_approval` / `auto_fix` / etc., drop `setup:`, add `auth:` block. Bump workflow pin to `<CURRENT_TAG>`, add secret pass-through.
  > - **Keep v0.2 setup-based config, just bump the version pin** — minimal change. Skip everything else.
  > - **Start fresh** — discard existing config and re-run the wizard from scratch.

- **Files exist with `auth:` block already, but pin is older than `<CURRENT_TAG>`** → `MODE=bump-only`. Patch the workflow to the latest pin and stop.

`MODE` decides what runs next:

| MODE | Skip Step 1–6 | Run Section 7 | Run Step 8c-pre (seed script) | Apply via |
|---|---|---|---|---|
| `fresh` | — | — | always | Step 1 onward |
| `legacy-migration` | yes | — | — | Section 0.5 migration block, then re-evaluate via the script |
| `needs-local-regen` | depends on the option the user picked | option-1: yes (re-runs Section 7) | option-1: yes; option-2/3: no | After AskUserQuestion: option-1 maps to `migrate` semantics; option-2 just emits the run-the-seed-script hint; option-3 falls through to bump-only |
| `migrate` | yes | yes | always | Section 7 + Section 8 patcher |
| `bump-only` (full v0.3) | yes | — | — | Section 8 patcher (version only) |
| `bump-only-with-seed` | yes | — | yes | Section 8 patcher + Step 8c-pre only |
| `current` | — | — | — | Print "PRoctor is already integrated and up to date" and stop |

For `migrate` and `bump-only`, **do NOT call out to Sections 1–6**; they're CI-bring-up flow only.

**Seed-script gating is orthogonal to MODE**: any time `.proctor/config.yml` declares `auth.accounts` AND no local seed script exists, Step 8c-pre runs. Bumping the action version doesn't help a dev who still needs to seed local users.

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

> "I'll add these `setup:` commands to `.proctor/config.yml`. OK?"
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

Write `.proctor/config.yml` at the repo root:

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
✓ .proctor/config.yml          (Vite + Go, dev servers on :5173 / :7000)
✓ .github/workflows/proctor.yml  (pinned to v0.2.0)
✓ Auth secret set: CLAUDE_CODE_OAUTH_TOKEN
✓ Actions PR-creation: enabled

Next:
  1. git add .proctor/config.yml .github/workflows/proctor.yml
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

In `MODE=migrate`, read the existing `.proctor/config.yml` first and use its values as DEFAULTS so the user can press-enter to keep what they have. Specifically, parse out: `require_approval`, `auto_fix`, `base_url` (if present), and `mobile_emulator`. Treat these as locked answers — don't re-ask.

### Step 7a — Login mechanism (Q-EnvA)

Ask, one short question via AskUserQuestion:

> "How does an admin log into the app you want PRoctor to test?"
- **Form: email + password + TOTP (2FA)** (Recommended) — sets `auth.type: form_with_totp`. Continue to 7b.
- **Form: email + password, no 2FA** — also `form_with_totp` schema-wise; mark the `totp` selector + `totp_seed_env` as TODO in the generated file, document for the user that the schema requires them but the runtime can treat 2FA as no-op when the seed is empty. Continue to 7b.
- **Something else** (SSO, magic link, OAuth) — skip the auth block. Tell the user PRoctor v0.3.0 doesn't generate other auth types yet; they can run without `auth:` (legacy mode) or hand-edit the file. Stop after generating the no-auth `.proctor/config.yml`.

### Step 7b — Login form selectors (Q-EnvB)

**Don't pre-fill from convention — read the actual login template.** qor/auth ships with one set of `name=` attributes, but every project overrides those templates and the attribute names drift. mcd-website uses `name="email"` (not `name="login"` which is the qor/auth default). Hard-coding the wrong values silently passes init but breaks login at runtime.

#### Step 7b.1 — find the login template

```bash
# Candidate files: anything that contains a password input and an
# email/login/username input. Covers Go html/template (.tmpl), Rails
# .erb, React .tsx/.jsx, Vue, Svelte, plain HTML.
LOGIN_TPL_CANDIDATES=$(
  grep -rln -E 'type="password".*type="(email|text)"|type="(email|text)".*type="password"' \
    --include='*.tmpl' --include='*.html' --include='*.htm' \
    --include='*.erb' --include='*.tsx' --include='*.jsx' --include='*.ts' --include='*.js' \
    --include='*.vue' --include='*.svelte' --include='*.go' \
    . 2>/dev/null | head -20

  # Single-file approach above can miss apps where email and password
  # inputs are in different files (e.g. multistep wizard). Also try:
  grep -rln -E '<input[^>]*type="password"' \
    --include='*.tmpl' --include='*.html' --include='*.htm' \
    --include='*.erb' --include='*.tsx' --include='*.jsx' \
    --include='*.vue' --include='*.svelte' . 2>/dev/null | head -20
)

# Filter by path heuristics — pick files whose path mentions auth / login / signin.
echo "$LOGIN_TPL_CANDIDATES" | grep -iE 'auth|login|signin|session' | head -5
```

If grep returns a candidate (or several), **Read each** with the Read tool — the candidates are template files, not gigantic.

#### Step 7b.2 — extract selectors from the actual form

For each form `<input>` you read, capture the `name=` attribute and classify by other attributes:

| input attribute | role |
|---|---|
| `type="password"` (no other strong signal) | `password` |
| `type="email"`, OR name matches `email`/`login`/`username`/`user` | `email` |
| name matches `passcode`/`totp`/`otp`/`2fa`/`token`/`code` AND length-6 ish | `totp` |
| `<button type="submit">` / `<input type="submit">` in same form | `submit` |

Same for the TOTP/2FA page if it's a separate URL — look for a single short-numeric input on whatever page comes AFTER successful email+password.

Example: applying the rules above to mcd-website's login template (the project that originally surfaced the rule) yields:
- `name="email"` → role: email
- `name="password"` → role: password
- `name="passcode"` (qor/auth's totp provider standard) → role: totp
- `<button type="submit">` → role: submit

A repo using a different framework or different field naming will land on different exact strings — the table above is the actual rule. Don't hardcode any of these literal `name=...` values; always read the consumer's actual template.

Record the detected values as `SELECTORS = {email, password, totp, submit, login_url}`. The `login_url` comes from how the form's `<form action="...">` is set, or you can keep `/auth/login` as a sane default if the form doesn't have an explicit action and the URL is what the user lands on.

#### Step 7b.3 — confirm via AskUserQuestion (cheap sanity check)

> "Detected login form selectors from `<TEMPLATE_FILE>`. Look right?"

Show the table inline with the detected values. Options:
- **Yes, use these** (Recommended)
- **No, let me edit** — opens a free-text input for any field the user wants to correct.

#### Step 7b.4 — fallback when detection fails

If `LOGIN_TPL_CANDIDATES` is empty (e.g. the project uses an SSO provider whose login page is hosted externally), tell the user honestly:

> "Couldn't find a login form template in this codebase — your login might be on an external SSO host. Manually open `<base_url>/auth/login` in DevTools, copy the `name=` attributes from each input, and type them below."

Then collect via four free-text questions. Don't silently pretend to know.

### Step 7c — Discover admin roles from the codebase

**Don't ask the user to count their roles — detect them.** Bias toward false positives (show everything found); the user will deselect what they don't care to test. **Multi-word snake_case role names like `system_administrator` and `internal_readonly` MUST be captured** — the regex needs to be word-char-permissive (`\w` / `[a-zA-Z0-9_]+`), not stop-at-second-underscore.

Two-pass detection:

#### Pass A — file-name-driven (high precision)

First locate the file(s) most likely to define roles, then read them:

```bash
ROLE_FILES=$(find . -type f \( \
    -name 'roles.go' -o -name 'role.go' -o \
    -name 'roles.py' -o -name 'role.py' -o \
    -name 'roles.rb' -o -name 'role.rb' -o \
    -name 'roles.ts' -o -name 'role.ts' \
  \) -not -path '*/vendor/*' -not -path '*/node_modules/*' 2>/dev/null | head -10)
```

If any are found, **read each** (Read tool, full file) and extract identifiers manually. Look for: `const Role_x = "y"`, `var Role_x = "y"`, enum members, hash keys, etc. This catches snake_case multi-word names a pure-regex grep often misses.

#### Pass B — pattern-driven grep (fallback / supplement)

Run all of these regardless of Pass A — they cover code that doesn't live in a `roles.*` file:

```bash
# Go (most permissive: any identifier starting with Role_ followed by word chars).
# Captures Role_developer, Role_system_administrator, Role_internal_readonly, etc.
grep -rhoE '\bRole_[a-zA-Z][a-zA-Z0-9_]*' \
  --include='*.go' . 2>/dev/null | sort -u | sed 's|^Role_||'

# Go (rolesPower-style maps, sometimes used alongside Role_* consts)
grep -rhoE '\brolesPower\s*\[\s*"[a-zA-Z][a-zA-Z0-9_]*"\s*\]' \
  --include='*.go' . 2>/dev/null | sed 's|.*"\(.*\)".*|\1|' | sort -u

# TypeScript / JS — enum body content + const ROLES array entries.
grep -rhoE '\b(enum\s+Role\s*\{[^}]+\})' \
  --include='*.ts' --include='*.tsx' --include='*.js' . 2>/dev/null
grep -rhoE '\bROLES\s*=\s*\[[^]]+\]' \
  --include='*.ts' --include='*.tsx' --include='*.js' . 2>/dev/null

# Python: class Role(Enum) blocks
grep -rhA20 -E '^\s*class\s+Role\b' --include='*.py' . 2>/dev/null \
  | grep -E '^\s*[A-Z][A-Z0-9_]*\s*=' | sed 's|=.*||' | tr -d ' ' | sort -u

# Ruby: role :xxx / has_role :xxx
grep -rhoE 'has_role\s+:[a-z][a-z0-9_]*' --include='*.rb' . 2>/dev/null \
  | sed 's|.*:||' | sort -u

# Database seeds / migrations / config — role names quoted
grep -rhoE '"[a-z][a-z0-9_]{2,30}"' \
  --include='*roles*.sql' --include='*role*.sql' \
  --include='*roles*.yml' --include='*role*.yml' \
  --include='*roles*.json' --include='*role*.json' \
  . 2>/dev/null | sed 's|"||g' | sort -u
```

### Merge into `DETECTED_ROLES` — Pass A wins, Pass B annotates only

**Pass A is authoritative when it finds a roles file.** If Pass A located `roles.go` (or `role.py` etc.) and successfully extracted ≥1 identifier from it, those identifiers ARE the complete list. Don't intersect with Pass B. Don't filter against rolesPower / permission tables / migration seeds — a role missing from a power-map is *still a role* (typically the read-only / unprivileged ones).

```
if PASS_A_IDENTIFIERS:
    DETECTED_ROLES = PASS_A_IDENTIFIERS
    # Pass B output is used ONLY to enrich descriptions:
    # e.g. "Role_developer (power 6) — full admin access"
else:
    DETECTED_ROLES = PASS_B_IDENTIFIERS
```

**Verification before showing the picker**: after building `DETECTED_ROLES`, re-grep the roles file for any `const ` / `var ` / enum members and confirm every one of them appears in `DETECTED_ROLES`. If the count differs, you missed some — add them back. Don't ask the user "did I miss any?"; the file is right there, the wizard's job is to read it correctly.

Filter rules (apply AFTER the union, not as part of authoritative selection):
- lowercase for dedup keying, but preserve the original casing/snake form for the eventual `auth.accounts[].name` value
- strip `Role_` / `ROLE_` prefix on display
- drop names matching `^(role|roles|user|users)$` (framework keywords)
- drop names containing `permission` / `migration` / `id` substring (false positives from messy greps)
- require display name matches `^[a-z][a-z0-9_]*$` (cleaning for picker)
- **never drop a name that came from Pass A** — even if it would have failed the above filters, prefer keeping it over silently losing a legitimate role

Present the result via AskUserQuestion (multi-select):

> "I found these candidate admin roles in the codebase. Which ones should PRoctor test under? (Each picked role becomes one auth account in `.proctor/config.yml`.)"
>
> Options: `<each DETECTED_ROLES entry>` + "Add a role not in this list" + "Just one generic admin account"

**Branching from this answer:**

- Picked ≥1 from the detected list → use those as the account names. Skip "Add a role not in this list" unless user explicitly clicked it.
- Picked "Add a role not in this list" → open a free-text follow-up: "Type the role name (will become `auth.accounts[].name`)". Loop until user types `done`.
- Picked "Just one generic admin account" → single account, name = `admin`.
- Nothing detected at all → present a different question: "What admin roles do you want to test? Type each name, one at a time. Type `done` when finished."

Save the final list as `ROLE_NAMES = [name, name, name]`.

`MODE=migrate` special case: if the existing `.proctor/config.yml` already has `auth.accounts:` declared, **skip the detection grep entirely** and confirm:

> "Existing `.proctor/config.yml` declares N accounts: `<names>`. Keep them, or rediscover from the codebase?"
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

In `MODE=migrate`: if the existing `.proctor/config.yml` already has `base_url`, ask to confirm (default = existing).

In `MODE=fresh`: ask via AskUserQuestion:

> "What URL is the deployed test environment at?"
Free-text input. Pre-fill example shape: `https://<service>.<env>.<your-org-dev-domain>` (e.g. `https://cms.example-app.acme-dev.com`). Validate:

- Must start with `https?://` (case-insensitive).
- REFUSE and re-ask (hard refusal — not a warning) when the URL contains any of: `prod.` / `production.` / `live.` (case-insensitive substring); or `www.<consumer-real-domain>.<tld>` (try to infer the production domain from `git remote get-url origin` — strip `.git`, take the org-or-repo hostname guess).
- Emit a notice that the wizard cannot be 100% certain whether a URL is prod and the user is responsible for not pointing PRoctor at production.

Save as `BASE_URL`.

### Step 7.5 — Multi-binary detection (v0.7.7+, fresh mode only)

**Why this step exists.** The v0.7.6 e2e against mcd-website PR #1126 found a real gap. The project ships multiple `cmd/*/main.go` binaries — the HTTP server (root `main.go`) AND `cmd/mcd-daemon/main.go` (a 1-minute ticker that republishes banners/categories to S3) AND one-shot CLI tools (`cmd/mcd-publisher`, `cmd/mcd-sitemap`). Pre-v0.7.7 `.proctor/local.yml setup:` ran `go run .` which started the HTTP server but NOT mcd-daemon. PRs that claimed "Published JSON include_tags/exclude_tags are arrays of trimmed tokens" couldn't be verified at runtime because mcd-daemon — the binary that DOES the publishing — wasn't running.

The fix: detect every `cmd/*/main.go` (plus the root `main.go`) at init time, classify each as `http-server` / `daemon` / `one-shot` / `unknown`, and ask the user which ones PRoctor should start during setup. Daemons selected here get appended to `setup:`, so they run during PRoctor invocations and produce output the planner can runtime-verify with a plain `curl`.

**Runs in `MODE=fresh` (full new install — the v0.7.7 path) AND `MODE=amend-daemons` (v0.7.8+ — existing consumer whose `setup:` lacks `go run ./cmd/` lines).** Skip in `MODE=migrate` (existing consumer; their `setup:` was already set up by hand) and `MODE=bump-only` (pin bump only).

**Procedure:**

1. Detect candidates:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wizard_detect_binaries.py \
       --repo-root .
   ```

   Output: `{"candidates": [{"path", "binary_name", "looks_like", "evidence"}, ...]}`. Empty `candidates` → skip the whole step (no Go binaries; this is a Node/Python/etc. repo).

2. Surface as AskUserQuestion (multi-select). Defaults:
   - All `http-server` entries are REQUIRED (can't deselect — the wizard re-adds them if the user tries).
   - All `daemon` entries are PRESELECTED (user can untick if they explicitly don't want a daemon in setup — e.g. it requires an external dep they can't easily run locally).
   - `one-shot` entries are NOT preselected.
   - `unknown` entries are NOT preselected (let the user decide).

   Question text:

   > "Detected these binaries under cmd/. Select which PRoctor should start as part of local setup.
   >
   > For projects with publish loops / cron / async workers, selecting their daemons here is what makes runtime verification possible — admin save → daemon publishes → PRoctor can curl the output URL.
   >
   > [REQUIRED] root main.go (http-server)
   > [recommended] cmd/<X>-daemon (looks like: daemon — ticker/job loop detected)
   > [optional] cmd/<X>-worker (looks like: daemon — workerqueue/goroutine detected)
   > [skip] cmd/<X>-publisher (looks like: one-shot — no ticker; run on-demand only)
   > [skip] cmd/<X>-sitemap (looks like: one-shot — short, no ticker)"

   Substitute the actual candidate paths and binary names from the detect output.

3. For each selected daemon (not the root http-server — that's covered by the existing Step 7f wait-loop pattern), generate two lines for `.proctor/local.yml setup:`:

   ```yaml
   - bash -c '[ -f /tmp/proctor-<NAME>.pid ] && kill "$(cat /tmp/proctor-<NAME>.pid)" 2>/dev/null; true'
   - bash -c 'set -a; . ./dev_env_local 2>/dev/null || . ./dev_env 2>/dev/null || true; set +a; nohup go run ./<PATH> > /tmp/proctor-<NAME>.log 2>&1 & echo $! > /tmp/proctor-<NAME>.pid'
   ```

   Where `<NAME>` is the binary's directory name (e.g. `mcd-daemon` from `cmd/mcd-daemon/main.go` → `proctor-mcd-daemon.pid`) and `<PATH>` is the candidate's `path` field. The pidfile names are scoped per-binary so multiple daemons don't collide.

4. After the daemon `go run` lines, add a final `sleep 3` line. Daemons run async and don't expose HTTP, so the wait-for-port loop pattern doesn't work — give them 3 seconds to get past their init (DB connection, config load, first ticker setup) before tests start.

5. The existing wait-for-port loop for the http-server (Step 7f) stays unchanged. The daemon lines slot in AFTER the http-server wait-loop and BEFORE the test execution begins.

**Wiring note for the state machine.** `MODE=fresh` still falls back to legacy SKILL.md prose for the full install. But v0.7.8 adds a NEW `MODE=amend-daemons` that the state machine drives end-to-end for an existing v0.7.6-era consumer (local.yml exists, `setup:` is non-empty, no `go run ./cmd/` line):

1. State `INIT` → `wizard_decide_mode.py` returns `mode=amend-daemons` → wizard emits `ask_user` (`header=Daemon scan`) offering Scan / Skip.
2. If user picks "Skip" → wizard emits `done`.
3. If user picks "Scan" → wizard emits a `bash` envelope running `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wizard_detect_binaries.py --repo-root . > /tmp/proctor-wizard-binaries.json` and the AI runs it.
4. After `--bash-rc 0` → wizard reads the JSON, sorts candidates (daemon → unknown → http-server → one-shot), emits `ask_user` (`header=Daemons to start in setup`, `multi_select=true`).
5. AI calls `AskUserQuestion` in multi-select mode. AI passes the user's picks back as `--answer "label1, label2"` (comma-separated). Wizard amends `.proctor/local.yml setup:` with the two-line group per pick (kill + start) via the helper `_amend_local_yml_with_daemons` (string-level edit; preserves comments; idempotent — re-running skips entries already in setup).
6. Wizard emits `done` with a summary citing the added binaries.

For `MODE=fresh` the legacy SKILL.md prose still applies (Step 7.5 procedure above runs in the AI's hands, then Step 8c-pre writes the seed script). State-machine integration of fresh-mode is deferred.

### Step 7f — Confirm setup commands + env source (v0.3.41+)

**Why this step exists**: previous wizard versions auto-generated `setup:` commands from stack detection and silently baked them into `.proctor/local.yml`. When detection was slightly wrong (the env-source file path, the build command, the right docker-compose path), the dev server would start in a misconfigured state and produce mysterious failures during `/proctor:proctor` runs (e.g. gRPC handshake errors talking to a backend with mismatched keys). The fix: show the proposed commands and ASK before writing.

This step has TWO sub-questions, both AskUserQuestion.

**7f.1 — Env-source file**

Search the repo for the file the dev source-imports before running the server. Candidates, in priority order:

```bash
ls -1 dev_env .envrc .env .env.local set-env.sh setup-env.sh 2>/dev/null
```

Render the proposed source command in chat:

```markdown
I'll source environment variables from `<top candidate>` before starting the server.
This file typically contains DB_HOST / DB_PORT / API keys / similar.
```

Then AskUserQuestion:

> "Which env-source file should the setup commands load before starting the server?"

Options (multi-select disabled — pick exactly one):
- `<top candidate>` — Recommended (auto-detected)
- `<each other candidate found>`
- "None — server doesn't need pre-sourced env vars"
- "Other" — free-text; user types a path

Store as `ENV_SOURCE_FILE` (may be empty string for "none").

**7f.2 — Setup commands preview**

Compose the proposed setup block using the snippets from Step 8b's "stack-aware setup commands" reference (docker compose if `COMPOSE_HIT`; sourcing `ENV_SOURCE_FILE`; pidfile kill; build + start by stack; wait-loop on the app port).

Render the FULL block in chat as a markdown YAML code-fenced block. Then AskUserQuestion:

> "Use these setup commands? They'll be written into your `.proctor/local.yml` (gitignored — only you have it)."

Options:
- "Use as-is — these look right" (Recommended)
- "Customize — I'll write my own"
- "Skip — leave `setup:` empty, I'll add commands later"

Branch:
- **Use as-is** → store the rendered commands as `SETUP_COMMANDS` (list of strings, one per line excluding YAML's `  - `).
- **Customize** → set `SETUP_COMMANDS = "<USER-FILLS-IN>"` marker. Step 8b / 8c-pre will write a `setup:` block with one TODO comment and stop there; the seed script writes `.proctor/local.yml` with the TODO comment in place of commands. Tell the user explicitly in the wizard summary: "Edit `.proctor/local.yml` to fill in your setup commands before running `/proctor:proctor`."
- **Skip** → set `SETUP_COMMANDS = []`. Same warning in the summary.

The confirmed `SETUP_COMMANDS` is what 8b and 8c-pre USE — they don't regenerate from detection at write time. Step 7f is the single source of truth for "what setup commands go into the dev's local yaml".

## 8. Apply changes (Sections 7 produces; this section commits to disk)

This section is reached at the end of `MODE=fresh` (existing-env path) OR `MODE=migrate` OR `MODE=bump-only`. It's the single file-mutation point. **Show the user a diff preview** before each Write — they can refuse any single change.

### 8a — Write `.proctor/config.yml`

For `MODE=fresh` or `MODE=migrate`: generate the YAML from Section 7's captured answers. Template:

```yaml
# Managed by /proctor-init. Re-run the wizard to regenerate.
# Per-developer overrides go in .proctor/local.yml (gitignored).

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

For `MODE=migrate`, DROP the existing `setup:` block (no longer needed in existing-env mode) and emit a `setup_removed` note to mention in the summary. Don't silently drop other keys we don't understand — preserve them at the end of the file with a `# Preserved from previous .proctor/config.yml:` comment.

### 8b — Write `.proctor/local.yml.example`

⚠ If Step 8c-pre's seed script is generated, **the dev should run that script
instead of copying this example file manually**. The script handles
generating TOTP seeds, seeding the local DB, AND writing `.proctor/local.yml`
for them. This example file is the FALLBACK (no seed script, custom flow).

The example file should include a `setup:` block so the developer gets auto-server-lifecycle out of the box. **Use the `SETUP_COMMANDS` confirmed in Step 7f** — do NOT regenerate from detection here. Step 7f is the single source of truth (user already saw + approved the commands). The snippet reference below remains for documentation purposes and for what Step 7f shows the user; this step just consumes the result.

Generate `.proctor/local.yml.example` like this:

```yaml
# Per-developer overrides. Copy this to .proctor/local.yml and edit.
# Gitignored — each dev keeps their own.
#
# How this works: every `claude /proctor:proctor <PR>` invocation runs
# `setup:` first, kills any previously-PRoctor-started server (tracked via
# pidfile), starts a fresh one with your current code, then runs scenarios.
# Edit code → re-run /proctor → automatic fresh-server cycle.

base_url: http://localhost:<APP_PORT>

setup:
  # < inject SETUP_COMMANDS from Step 7f here; if the user picked
  #   "Customize" or "Skip" at 7f.2, emit a single TODO line:
  #   - # TODO: fill in your setup commands (e.g. docker compose up, server start)
  # >

  # Reference (what Step 7f offered the user — the wizard built this from
  # stack detection then showed it in chat for confirmation):

teardown:
  # Default: leave the server running so you can keep iterating in your
  # browser between PRoctor runs. PRoctor will kill+restart it on the
  # NEXT /proctor invocation via the pidfile in setup.
  []

# Override credentials only if your local dev env has its own ai-tester accounts:
# (uncomment + edit; the whole `accounts:` array REPLACES the base on merge)
#
# auth:
#   accounts:
#     - name: developer
#       role_label: "Local dev (full admin)"
#       email_env: LOCAL_DEV_EMAIL
#       password_env: LOCAL_DEV_PASSWORD
#       totp_seed_env: LOCAL_DEV_TOTP_SEED
```

The `setup:` block content depends on `STACK_SUMMARY`. Compose the lines by
appending the applicable snippets below in order (idempotent restart pattern
— PRoctor kills its own previous PID via the pidfile, never the dev's
unrelated processes):

**1. Bring up infra dependencies** (always emit if `COMPOSE_HIT` is set):
```yaml
  - docker compose -f <COMPOSE_HIT> up -d
  - bash -c 'for i in $(seq 1 30); do nc -z localhost <DB_PORT> && break; sleep 1; done'
```

If `DB_PROVISION=services` (the rare case), skip this snippet — local has its
own DB so `services:` only applies in CI.

**2. Kill previous PRoctor-managed app server** (always emit):
```yaml
  - bash -c '[ -f /tmp/proctor-<REPO_NAME>.pid ] && kill "$(cat /tmp/proctor-<REPO_NAME>.pid)" 2>/dev/null; true'
```

`REPO_NAME` = the repo's basename (e.g. `mcd-website`), so multiple PRoctor-managed projects don't collide on pidfile.

**3. Build + start the app server** — pick by stack:

For **Node / Vite / Next**:
```yaml
  - corepack enable && corepack prepare pnpm@9 --activate
  - pnpm install --frozen-lockfile
  - bash -c 'nohup pnpm dev > /tmp/proctor-<REPO_NAME>.log 2>&1 & echo $! > /tmp/proctor-<REPO_NAME>.pid'
```

For **Go modules**:
```yaml
  - go mod download
  - bash -c 'set -a; . ./dev_env 2>/dev/null || true; set +a; nohup go run . > /tmp/proctor-<REPO_NAME>.log 2>&1 & echo $! > /tmp/proctor-<REPO_NAME>.pid'
```

If the repo has a `dev_env` (or `.env`, `dev_env_local`, `.envrc`) file at the root, source it before `go run` to pick up the right ports / DB creds. Sources are tried in that order; the first one that exists wins. Skip sourcing silently if none exist.

For **GOPATH-era Go** (no go.mod):
```yaml
  - mkdir -p "$HOME/go/src/<IMPORT_PATH%/*>" && ln -sfn "$PWD" "$HOME/go/src/<IMPORT_PATH>"
  - cd "$HOME/go/src/<IMPORT_PATH>" && go get -d -v ./... || true
  - bash -c 'cd "$HOME/go/src/<IMPORT_PATH>" && set -a; . ./dev_env 2>/dev/null || true; set +a; nohup go run . > /tmp/proctor-<REPO_NAME>.log 2>&1 & echo $! > /tmp/proctor-<REPO_NAME>.pid'
```

For **Python / Django / FastAPI / Rails / etc.** — emit a TODO placeholder:
```yaml
  # TODO: fill in your local server start. Pattern:
  # - <install deps>
  # - bash -c 'nohup <run-server> > /tmp/proctor-<REPO_NAME>.log 2>&1 & echo $! > /tmp/proctor-<REPO_NAME>.pid'
```

**4. Wait loop on the app's port** (always emit):
```yaml
  - bash -c 'for i in $(seq 1 60); do curl -fsS http://localhost:<APP_PORT>/<HEALTH_PATH> >/dev/null 2>&1 && break; sleep 1; done || { echo "server failed to come up"; tail -50 /tmp/proctor-<REPO_NAME>.log; exit 1; }'
```

`HEALTH_PATH` = `auth.login_url` rewritten without leading slash (it's a route the app actually serves, so this both verifies the binary booted AND that templates render). E.g. `auth.login_url: /auth/login` becomes `auth/login`.

**5. Append a comment block reminding the dev about the iteration cycle**:
```yaml
# Iteration cycle:
#   1. Edit code in your editor.
#   2. Run: claude /proctor:proctor <PR#>
#   3. PRoctor kills the previous server, builds + starts a fresh one,
#      runs scenarios, posts the report.
#   4. Repeat — keep your editor open, PRoctor handles the rest.
```

### 8c-pre — Write the local-seed helper script

**Runs whenever `NEEDS_SEED_SCRIPT=yes`** (i.e. `auth.accounts` declared but no `.proctor/seed-local.sh` / `.proctor/seed-local.sh` / top-level equivalent exists yet). Orthogonal to MODE — runs in fresh, migrate, AND bump-only-with-missing-script.

If the seed script already exists, **don't overwrite** — just skip this step. The dev's filled-in TODO block (their project-specific UPSERT SQL) is in there; clobbering would discard that work.

Generate `.proctor/seed-local.sh` (or `.proctor/seed-local.sh` if no `hack/`, or top-level `proctor-seed-local.sh` if neither exists). This script:

1. Generates a fresh 32-char base32 TOTP seed per account.
2. Inserts/upserts each account into the local DB.
3. Writes `.proctor/local.yml` with the resulting credentials as **inline values** (not env vars — the file is gitignored, so plaintext is acceptable for local-only test accounts).

**Before writing the script template, the wizard MUST do three reads:**

#### Read 0: derive the actual UPSERT SQL from the user model

Don't leave SQL as a TODO. Read the codebase, figure out the schema, generate the statement. Steps the wizard runs *with the Read tool*, in order:

1. **Find the user model file.** Look for:

   ```bash
   # Files most likely to define the admin/user model
   find . -type f \( \
       -name 'user.go' -o -name 'admin_user.go' -o -name 'user_model.go' \
     \) -not -path '*/vendor/*' -not -path '*/node_modules/*' 2>/dev/null

   # Or grep for gorm-tagged structs with email/password fields
   grep -rln -E 'gorm:"[^"]*" *json:|json:"[^"]*" *gorm:|Email.*\`gorm:|Password.*\`gorm:' \
     --include='*.go' . 2>/dev/null | head -10

   # Or where the auth flow registers the user model — usually in boot/auth code
   grep -rln 'auth_identity|RegisterProvider.*password' --include='*.go' . 2>/dev/null | head -5
   ```

   When you find candidates, **Read each candidate file in full** (Read tool, not grep). Identify:
   - Struct name (e.g. `User`, `AdminUser`)
   - Table name (from `TableName() string` method, or default = snake_plural of struct name)
   - Column for **email/login** (likely `Email`, `Login`, `Username`)
   - Column for **password hash** (likely `Password`, `EncryptedPassword`, `PasswordHash`)
   - Column for **role** (likely `Role`, `RoleID`, `Roles` for many-to-many)
   - Column for **TOTP secret** (likely `TOTPSecret`, `OTPSecret`, `TwoFactorSecret`)
   - Whether `gorm.Model` is embedded (gives `id, created_at, updated_at, deleted_at`)

2. **Find the migration / table-creation source.** Check `database/migration/` / `migrations/` / similar. Verify the column types and any NOT-NULL / DEFAULT constraints. If the migration is `db.AutoMigrate(&User{})`, the gorm tags from step 1 ARE the schema.

3. **Determine password hash function.** Look at how the app stores passwords. For qor/auth password provider this is bcrypt with the cost set in code. Patterns:

   ```bash
   grep -rEn 'bcrypt\.(Generate|Hash|GenerateFromPassword)' --include='*.go' . 2>/dev/null | head -5
   grep -rEn 'password_themes/clean|qor/auth/providers/password' --include='*.go' . 2>/dev/null | head -3
   ```

   For qor/auth / clean theme: bcrypt at the default cost (10).

   **DO NOT assume Python's `bcrypt` module is installed** — it's not in the stdlib. Use the bcrypt that's already on the developer's machine via the project's own toolchain:

   - **If the project is Go** (`go.mod` exists): generate the hash via a tiny inline Go program using `golang.org/x/crypto/bcrypt` — the same library the app uses, and it's already in go.sum because qor/auth pulls it transitively. No new dep.

     **The temp dir MUST live INSIDE the project tree** (not `$TMPDIR` / `/var/folders/...`) so that `go run` can walk up and find the project's `go.mod`. On macOS specifically `mktemp -d -t` lands in `/var/folders/...` and `go run` fails with "no required module provides package...". Use a relative template starting with `./`:

     ```bash
     gen_hash() {
       local tmpdir
       tmpdir=$(mktemp -d "./.proctor-hash-XXXXXX")
       trap "rm -rf '$tmpdir'" RETURN
       cat > "$tmpdir/main.go" <<'GO'
     package main
     import ("fmt"; "os"; "golang.org/x/crypto/bcrypt")
     func main() {
       h, err := bcrypt.GenerateFromPassword([]byte(os.Args[1]), 10)
       if err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
       fmt.Println(string(h))
     }
     GO
       go run "$tmpdir/main.go" "$1"
     }
     ```

     The wizard ALSO appends `.proctor-hash-*/` to `.gitignore` so an interrupted run (Ctrl+C bypasses the trap) doesn't litter `git status`.

   - **If the project is Node** (`package.json`): `npx -y bcrypt-cli "$1" 10`.
   - **If the project is Python** (`pyproject.toml` / `requirements*.txt`): inline a check — if `python3 -c 'import bcrypt'` succeeds, use it; otherwise `pip3 install --user bcrypt` then retry, with a friendly error message if both fail.
   - **Otherwise** (Rust / Ruby / etc.): fall back to `htpasswd -bnBC 10 "" "$1" | tr -d ':\n'` if `htpasswd` exists (Apache utils — usually pre-installed on macOS / Linux); otherwise emit a comment that the dev needs to install one of: `htpasswd`, `python3-bcrypt`, or `bcrypt-cli`.

   The wizard inlines whichever helper matches the detected stack into the seed script.

4. **For TOTP, check whether the app expects the secret stored as base32, raw bytes, or already-decoded.** Search for how the existing login validates 2FA:

   ```bash
   grep -rEn 'totp\.(Validate|GenerateCode|GenerateOpts)|otp\.NewKeyFromURL|base32\.Std.*Decode' \
     --include='*.go' . 2>/dev/null | head -10
   ```

   For `pquerna/otp/totp` (the de-facto Go library), the secret is stored as the base32 string — same form we generated. No conversion needed.

5. **Assemble the SQL.** Plug everything in:

   ```sql
   -- USER_MODEL_TABLE = "<extracted table name, e.g. admin_users>"
   -- columns from the gorm/migration read:
   INSERT INTO <table> (
       <email_col>, <password_col>, <role_col>, <totp_col>,
       created_at, updated_at
   ) VALUES (
       $1, $2, $3, $4, now(), now()
   )
   ON CONFLICT (<email_col>) DO UPDATE SET
       <password_col> = EXCLUDED.<password_col>,
       <role_col>     = EXCLUDED.<role_col>,
       <totp_col>     = EXCLUDED.<totp_col>,
       updated_at     = now();
   ```

   If the table doesn't have a unique constraint on `<email_col>`, fall back to a two-statement `DELETE ... ; INSERT ...` block (idempotent, less elegant).

6. **Insert the password as a PRE-HASHED string** in the SQL, not as plaintext. The seed script calls `gen_hash` BEFORE passing `$2` to `psql`. So the script's `upsert_user` body becomes:

   ```bash
   upsert_user() {
     local email="$1" plain_password="$2" role="$3" totp_seed="$4"
     local hashed
     hashed="$(gen_hash "$plain_password")"
     PGPASSWORD="$<DB_PASSWORD_VAR>" psql -h "$<DB_HOST_VAR>" -p "$<DB_PORT_VAR>" \
       -U "$<DB_USER_VAR>" -d "$<DB_NAME_VAR>" <<SQL
       INSERT INTO <users_table> (<email_col>, <password_col>, <role_col>, <totp_col>, created_at, updated_at)
       VALUES ('$email', '$hashed', '$role', '$totp_seed', now(), now())
       ON CONFLICT (<email_col>) DO UPDATE
         SET <password_col> = EXCLUDED.<password_col>,
             <role_col>     = EXCLUDED.<role_col>,
             <totp_col>     = EXCLUDED.<totp_col>,
             updated_at     = now();
   SQL
   }
   ```

   Substitute `<users_table>`, `<email_col>`, `<password_col>`, `<role_col>`, `<totp_col>` with what Steps 1-2 found in the consumer repo. Substitute `<DB_*_VAR>` with the actual env var names from the consumer's `dev_env` / `.env` (e.g. `MCD_DB_*` / `POSTGRES_*` / `DB_*` — whatever the project uses).

7. **If anything in steps 1-4 is ambiguous** (multiple candidate user models, both TOTPSecret and OTPSecret columns, no migration to verify against), do NOT silently pick one. Present the candidates via AskUserQuestion:

   > "I found two candidate user models: <fileA> and <fileB>. Which one is the admin login?"

   Or for ambiguous columns:

   > "Found columns: email | login. Which is used for admin login?"

   Don't ship a guessed SQL — ask once and bake in the answer.

Save `GENERATED_UPSERT_SQL` for use in the template below.

#### Read 1: detect the consumer's email domain convention

Look for the project's actual email style — don't make up a `local.test` placeholder when the project clearly uses its own. Try these in order, take the first that returns a hit:

```bash
# Existing admin / user emails in test fixtures, dev_env, README, CLAUDE.md
grep -rhoE '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}' \
  README.md CLAUDE.md dev_env dev_env_local 2>/dev/null \
  | grep -vE '(noreply|no-reply|example\.com|test\.com|@local\.test)' \
  | sort -u | head -5

# Author emails on recent commits (likely match company domain)
git log --pretty='%ae' -200 2>/dev/null | sort -u | grep -vE 'github|noreply' | head -10

# package.json / go.mod author / repo owner
gh repo view --json owner --jq '.owner.login' 2>/dev/null
```

From whatever surfaces, derive `EMAIL_DOMAIN` — pick the most-common-suffix found across the consumer's existing user fixtures (e.g. if grep finds multiple `@example-corp.com` matches → `example-corp.com`). Show it to the user via AskUserQuestion:

> "I'll create test accounts as `ai-tester-<role>@<EMAIL_DOMAIN>`. Use `<DETECTED_DOMAIN>`?"
- **Use `<DETECTED_DOMAIN>`** (Recommended)
- **Custom domain** — open free-text input.

Save as `EMAIL_DOMAIN`. The generated email template becomes `ai-tester-<role-name>@<EMAIL_DOMAIN>` — descriptive, clearly distinct from real user emails, matches the project's existing convention.

#### Read 2: detect the password requirements from the auth code

A hard-coded `proctor-local-dev` will fail apps with strict password validators (length ≥ 12, mixed-case, special chars). Read the app's auth code to figure out what it actually requires.

```bash
# Look for password validator funcs and their constraints.
# qor/auth, devise, passlib, bcrypt, argon2 — find the constraint definitions.
grep -rEnh '(min_?length|password_?(min|min_length|complexity)|len\(password\)\s*<|MinLength|len\([^)]*\)\s*>=)' \
  --include='*.go' --include='*.rb' --include='*.py' --include='*.ts' . 2>/dev/null \
  | head -10

# Bcrypt cost / argon2 params / sha hashing — tells us how to hash for the SQL
grep -rEnh '(bcrypt\.(Generate|Hash)|argon2|password_hash|crypt\(.*bf|Devise\.bcrypt_cost)' \
  --include='*.go' --include='*.rb' --include='*.py' . 2>/dev/null | head -5

# qor/auth specifically — uses bcrypt by default, no built-in complexity rules.
grep -rh 'qor/auth/providers/password' --include='*.go' . 2>/dev/null | head -2
```

Form a tentative `PASSWORD_RULES` dict like `{min_length: 8, hash: "bcrypt", complexity_required: false}`. Then ask the user via AskUserQuestion:

> "Detected password rules from auth code: min_length=<N>, hash=<bcrypt|argon2|...>, complexity=<yes|no>. Generate test passwords matching these rules?"
- **Use detected rules** (Recommended)
- **Edit rules** (opens text fields)
- **No rules detected — use a sensible default** (length 16, mixed alphanumeric, no special chars) — shown only when grep found nothing.

Use `PASSWORD_RULES` to generate a password the seed script bakes in. For each account, the password can be the same (one local-only value) or per-role unique (more secure but more state to track). v0.3.6 used same-for-all; keep that.

The DB-insertion step is genuinely project-specific (table name, password hashing scheme, role column name). The wizard emits a SCAFFOLD with clear TODO markers; the dev fills them in once. Template:

```bash
#!/usr/bin/env bash
# Generated by /proctor-init. Re-run any time to refresh local AI-tester accounts.
# Idempotent: subsequent runs UPSERT, no duplicates.
#
# What this does:
#   1. Generate a fresh TOTP seed per role.
#   2. UPSERT each role's user into your local DB.
#   3. Write .proctor/local.yml with the resulting credentials inline.
#
# Run after `docker-compose up` so your local DB is reachable.
set -euo pipefail
cd "$(dirname "$0")/.."

# Source dev env so we know DB connection info (host/port/user/db).
# Comment out if your project uses a different mechanism.
[ -f dev_env ] && set -a && . ./dev_env && set +a

gen_seed() {
  # 20-byte secret → 32-char base32, padding stripped (Google Authenticator format)
  python3 -c "import secrets, base64; print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip('='))"
}

# Generated password: meets the rules detected from the app's auth code
# (see PASSWORD_RULES captured during /proctor-init). Override via
# PROCTOR_SEED_PASSWORD if your CI / sandboxes prefer a different one.
PASSWORD="${PROCTOR_SEED_PASSWORD:-<GENERATED_PASSWORD_FROM_PASSWORD_RULES>}"

# Use parallel indexed arrays instead of `declare -A` (associative arrays).
# macOS ships bash 3.2 as /bin/bash which doesn't support associative arrays;
# `declare -A` silently no-ops there and `[key]=` is then interpreted as
# arithmetic indexing → "unbound variable" under set -u. Parallel arrays
# work on bash 3.2 through 5.x identically.
ROLES=(<list of ACCOUNTS[i].name, space-separated>)
EMAILS=(
<for each ACCOUNTS[i]:>
  "ai-tester-<ACCOUNTS[i].name>@<EMAIL_DOMAIN>"
</for>
)
SEEDS=()  # filled in the loop below — one entry per ROLES[i]

# === BEGIN: upsert_user generated by reading the app's code ===
# Wizard inspected the codebase before generating this. If a column or
# table name is wrong, edit the SQL below — but PRoctor tried to match
# what's actually in your models / migrations / auth setup.
#
# Sources consulted:
#   - <USER_MODEL_FILE>          ← struct + gorm tags
#   - <MIGRATION_FILE>           ← CREATE TABLE columns (if any)
#   - <AUTH_SETUP_FILE>          ← password hashing scheme
#
upsert_user() {
  local email="$1" password="$2" role="$3" totp_seed="$4"
  PGPASSWORD="$<DB_PASSWORD_ENV>" psql -h "$<DB_HOST_ENV>" -p "$<DB_PORT_ENV>" \
    -U "$<DB_USER_ENV>" -d "$<DB_NAME_ENV>" <<SQL
<GENERATED_UPSERT_SQL>
SQL
}
# === END: upsert_user ===

HASHED_PASSWORD="$(gen_hash "$PASSWORD")"

# Iterate by INDEX (bash 3.2-compatible — works on macOS's stock /bin/bash).
for i in "${!ROLES[@]}"; do
  role="${ROLES[$i]}"
  email="${EMAILS[$i]}"
  seed="$(gen_seed)"
  SEEDS[$i]="$seed"
  upsert_user "$email" "$HASHED_PASSWORD" "$role" "$seed"
  printf '  ✓ %-22s %s\n' "$role" "$email"
done

# Emit .proctor/local.yml with inline credentials AND setup commands so
# PRoctor brings up the local server itself on `claude /proctor:proctor`.
# (Gitignored — see .gitignore.)
{
  echo "# Generated by .proctor/seed-local.sh — DO NOT COMMIT."
  echo "# Re-run that script to regenerate (TOTP seeds rotate each run)."
  echo
  echo "base_url: http://localhost:<APP_PORT>"
  echo
  echo "# setup: runs before every /proctor:proctor invocation. PRoctor"
  echo "# kills any server it previously started (via pidfile), starts a"
  echo "# fresh one with your current code, waits for the login page to"
  echo "# respond, then logs in. You don't need to manually 'go run'."
  echo "setup:"
<v0.3.41+: emit the `SETUP_COMMANDS` list that Step 7f confirmed.
If SETUP_COMMANDS is empty (user picked "Customize" or "Skip"), emit
ONE TODO line so the YAML stays valid + the dev knows to fill in:
  echo "  # TODO: fill in your setup commands (e.g. docker compose up, server start)"
  echo "  # The wizard skipped auto-generation per your choice at Step 7f.2."
Otherwise emit one `  - <cmd>` per entry:>
<for each line in SETUP_COMMANDS:>
  echo "  - $LINE"
</for>
  echo
  echo "auth:"
  echo "  accounts:"
  for i in "${!ROLES[@]}"; do
    echo "    - name: ${ROLES[$i]}"
    echo "      email: ${EMAILS[$i]}"
    echo "      password: $PASSWORD"
    echo "      totp_seed: ${SEEDS[$i]}"
  done
} > .proctor/local.yml

echo
echo "✓ Wrote .proctor/local.yml ($(wc -l < .proctor/local.yml | tr -d ' ') lines)"
echo "✓ ${#ROLES[@]} local AI-tester accounts seeded into $<DB_NAME_ENV>."
echo
echo "Next: claude /proctor:proctor <PR#>"
```

Substitute the wizard's discovered values for `<ACCOUNTS[i].name>`, `<APP_PORT>`, and the database env names (project-specific — e.g. `MCD_DB_*`, `POSTGRES_*`, `DB_*` — read from the consumer's `dev_env` / `.env` / docker-compose). When generating the loop body, emit one line per role (don't leave a literal `<for each>` block).

Make the script executable: `chmod +x <path-to-script>`.

#### `STACK_AWARE_SETUP_COMMANDS` to embed in `.proctor/local.yml`

Re-use the same template from Section 8b's `.proctor/local.yml.example` setup block — same pidfile pattern, same wait-loop, same stack-specific build/run command. Compose the list:

```
- docker compose -f <COMPOSE_HIT> up -d              (if COMPOSE_HIT)
- bash -c 'for i in $(seq 1 30); do nc -z localhost <DB_PORT> && break; sleep 1; done'  (if COMPOSE_HIT)
- bash -c '[ -f /tmp/proctor-<REPO_NAME>.pid ] && kill "$(cat /tmp/proctor-<REPO_NAME>.pid)" 2>/dev/null; true'
- <build commands per stack — go mod download / pnpm install / pip install -e .>
- bash -c '<source-dev-env if exists> nohup <run command> > /tmp/proctor-<REPO_NAME>.log 2>&1 & echo $! > /tmp/proctor-<REPO_NAME>.pid'
- bash -c 'for i in $(seq 1 60); do curl -fsS http://localhost:<APP_PORT><AUTH_LOGIN_URL> >/dev/null 2>&1 && break; sleep 1; done || { tail -50 /tmp/proctor-<REPO_NAME>.log; exit 1; }'
```

These commands go INSIDE the seed script's heredoc (each preceded by `echo "  - ..."`) and **escape the heredoc properly**: use a single-quoted heredoc delimiter (`<<'YAML'`) for the YAML emission OR consistently escape `$` to `\$` where the bash expansion should happen at PRoctor RUN time, not at seed-script run time. Concretely: `$(seq 1 30)`, `$i`, `$(cat ...)` in the setup commands must REMAIN AS LITERAL TEXT inside the YAML so PRoctor's executor can run them later — they're not meant to expand when the seed script writes the file.

To make that bulletproof, generate the setup commands as a single quoted heredoc'd block:

```bash
# REPO_NAME, APP_PORT, AUTH_LOGIN_URL, DB_PORT come from the wizard's
# earlier discovery; substitute them BEFORE feeding to the heredoc so
# the literal values land in the emitted YAML.
SETUP_BLOCK=$(cat <<YAML
  - docker compose -f docker-compose.yml up -d
  - bash -c 'for i in \$(seq 1 30); do nc -z localhost <DB_PORT> && break; sleep 1; done'
  - bash -c '[ -f /tmp/proctor-${REPO_NAME}.pid ] && kill "\$(cat /tmp/proctor-${REPO_NAME}.pid)" 2>/dev/null; true'
  - go mod download
  - bash -c 'set -a; . ./dev_env 2>/dev/null || true; set +a; nohup go run . > /tmp/proctor-${REPO_NAME}.log 2>&1 & echo \$! > /tmp/proctor-${REPO_NAME}.pid'
  - bash -c 'for i in \$(seq 1 60); do curl -fsS http://localhost:${APP_PORT}${AUTH_LOGIN_URL} >/dev/null 2>&1 && break; sleep 1; done || { tail -50 /tmp/proctor-${REPO_NAME}.log; exit 1; }'
YAML
)
```

`${REPO_NAME}` / `${APP_PORT}` / `${AUTH_LOGIN_URL}` / `<DB_PORT>` are placeholders — the wizard substitutes them at generation time. The `\$` escapes preserve `$i`, `$(seq ...)`, `$(cat ...)` as literal text inside the emitted YAML so PRoctor's executor can run them later.

Then in the YAML emission section: `echo "$SETUP_BLOCK"` to dump it verbatim. The single-quoted heredoc preserves `$i`, `$(seq ...)`, `$(cat ...)` as literal text.

### 8c — Update `.gitignore`

Read `.gitignore`. For each of these lines, append it ONLY if not already present:

```
.proctor/local.yml
.proctor/runs/
.proctor/wizard-state.json
.proctor-hash-*/
```

Note: only the four paths above. **Do NOT** add a bare `.proctor/` rule — that would silently gitignore the files the consumer is supposed to commit (`config.yml`, `local.yml.example`, `seed-local.sh`).

Breakdown:
- `.proctor/local.yml` — per-developer credentials (TOTP seeds, passwords). Never committed.
- `.proctor/runs/` — per-run artifacts (logs, screenshots, reports). Disposable.
- `.proctor/wizard-state.json` — transient state machine file for `/proctor:proctor-init`. v0.7.3+ auto-deletes on `step=done`; gitignore protects the resume-after-crash case.
- `.proctor-hash-*/` — in-tree temp dirs the Go-stack `gen_hash` helper creates during seed-script runs. The script's `trap RETURN rm -rf` cleans them up under normal exit; this gitignore line covers the Ctrl+C case.

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
  ✓ .proctor/config.yml                           (created / updated)
  ✓ .proctor/local.yml.example             (created — copy to .proctor/local.yml)
  ✓ .gitignore                             (.proctor/ and .proctor/local.yml ignored)
  ✓ .github/workflows/proctor.yml          (action pinned to <CURRENT_TAG>, secrets pass-through added)

Next steps:
  1. Commit the above and open a PR (or merge if you've batched it on master).
  2. (CI / deployed env) Run the secret-set commands above for each account.
  3. (Local dev) Run the seed script to populate your local DB + generate
     `.proctor/local.yml`:
        ./.proctor/seed-local.sh
     (Replace path if generated elsewhere. The script TODO-marks the SQL
     part — fill that in once per project, then `.proctor/local.yml` writes
     itself on every re-run.)
  4. (If migration) The old setup: block was dropped — see PRESERVED comment block
     at the bottom of .proctor/config.yml for anything else that wasn't recognized.
  5. Test: comment `/proctor run` on any PR (CI), or `claude /proctor:proctor <PR#>` locally.
```

If any step was skipped (auth not generated, perms not flipped, secrets not set), call those out as "TODO" lines so the user remembers.

## Style guide

- Don't ask questions you can already answer (skip Q5 if `CLAUDE_CODE_OAUTH_TOKEN` is already set on the repo; skip Q2.5 if `DB_NEEDED=false`).
- Don't paste large config blocks unless asked — show the diff or path, not the body.
- If anything errors mid-flow, report the failure plainly and let the user re-run.
- Never proceed past pre-flight if `gh` isn't authenticated.

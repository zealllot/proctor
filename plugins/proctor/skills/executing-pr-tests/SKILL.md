---
name: executing-pr-tests
description: Use after the approval gate to dispatch each item in an ApprovedPlan to the pr-test-executor subagent and assemble TestResults. Third stage of the PRoctor pipeline. Output is a single TestResults JSON object — no prose.
---

# Executing PR Tests

Input: `approved-plan.json` (a TestPlan filtered by the approval gate),
plus environment from the command (run-id, logs dir, base_url, timeout
seconds, head_sha, repo).

Output: a single `TestResults` JSON object.

## Procedure

1. **Re-fetch PR head SHA** with `gh pr view <PR#> --json headRefOid`.
   If it differs from the cached `head_sha` → abort the run, print
   `[proctor:execute] aborted reason=force-push` to stdout, return:

   ```jsonc
   {"items": [], "summary": {"total": 0, "pass": 0, "fail": 0, "skipped": 0},
    "aborted": "force-push"}
   ```

   The command-level orchestrator will detect `aborted` and skip fix +
   replace report with a force-push notice.

2. **Align to PR head via worktree** (v0.3.37+, local mode only). Before running setup, check whether the dev's checkout is at the PR's head SHA. If it isn't, set up an ephemeral worktree so the dev server compiles + runs PR's code, not the user's current branch's code.

   ```bash
   cur_head=$(git rev-parse HEAD)
   pr_head="<value from pr.head_sha>"
   if [ "$cur_head" = "$pr_head" ]; then
       WORKTREE_DIR="$(pwd)"   # already aligned, no worktree needed
   else
       # Read .proctor/config.yml.worktree_symlink_dirs (a list of dirs
       # to symlink from the main checkout into the worktree so the dev
       # server doesn't have to rebuild gitignored runtime artifacts).
       # If unset → omit --symlink-dirs so worktree.py uses its built-in
       # default list. If set to [] → pass empty string to skip all.
       # If set to a list → join with commas.
       SYMLINK_ARGS=()
       if [ -n "${PROCTOR_WORKTREE_SYMLINK_DIRS+x}" ]; then
           SYMLINK_ARGS=(--symlink-dirs "$PROCTOR_WORKTREE_SYMLINK_DIRS")
       fi
       WORKTREE_DIR=$(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree.py setup \
           --run-dir .proctor/runs/<run-id> \
           --pr-number <pr.number> \
           --head-sha "$pr_head" \
           "${SYMLINK_ARGS[@]}")
   fi
   ```

   The helper:
   - Fetches `pull/<n>/head` if the SHA isn't already in local objects.
   - Creates a detached-HEAD worktree at `.proctor/runs/<run-id>/pr-checkout/`.
   - Copies `.proctor/local.yml` from the original repo into the worktree (it's gitignored, so it wouldn't otherwise be present — but the dev's setup commands + credentials live there).
   - Prints the absolute worktree path on stdout.

   On failure (PR force-pushed since plan was captured, fetch unavailable, etc.), the helper raises; abort the run with `aborted: "worktree-setup-failed"`.

   In CI mode (no local dev server — `auth:` present, `setup:` empty), skip this step entirely. The CI test env is already deployed at the PR's SHA via the workflow.

2b. **Bring up the dev environment** from the merged config, with cwd set to `$WORKTREE_DIR`. Each command via Bash.

   The merged config is `.proctor/config.yml` overlaid by `.proctor/local.yml`
   when the latter exists (the file is gitignored — only developers
   running PRoctor locally will have one). Precedence (v0.7.11+):

   1. **`dev_launcher` block** (recommended, new in v0.7.11). When
      `.proctor/config.yml.dev_launcher.start` is set, run that ONE
      command via Bash. Then, when `dev_launcher.wait_for` is set,
      poll it in a loop (every 1s, exit 0 wins) until either it
      succeeds or `wait_timeout_seconds` elapses (default 60).
      When `wait_for` is unset, sleep 2s as a basic settling pause.
      If `start` exits non-zero OR the readiness poll times out →
      abort with `aborted: "dev-launcher-failed"`.

   2. **Legacy `setup:` array** (v0.7.10-and-earlier, preserved).
      When `dev_launcher` is absent but `.proctor/config.yml setup:`
      or `.proctor/local.yml setup:` is a non-empty list, run each
      command in order via Bash. If any exits non-zero → abort
      with `aborted: "setup-failed"`.

   3. **Neither configured** → error: "PRoctor doesn't know how to
      start your dev env. Run `/proctor:proctor-init` and configure
      either `dev_launcher` or `setup:`."

   In practice:

   - **CI runs** (deployed test env, `auth:` present, neither
     `dev_launcher` nor `setup:`) → bring-up is empty, skip this
     step. Auth in step 3 logs into the already-deployed server.
   - **Local runs with dev_launcher** (recommended) → `start` runs
     once, readiness check passes, tests run. After tests `stop`
     runs (if set) regardless of pass/fail.
   - **Local runs with legacy `setup:`** → setup commands run as
     before. No auto-teardown (the legacy path never had one — the
     consumer's own setup typically embeds a "kill previous
     PRoctor-managed PID via pidfile" idiom for re-runs).

   Don't try to be clever about which path to pick — read
   `dev_launcher` first; if absent, fall through to `setup:`. Both
   paths can coexist on disk during a migration; `dev_launcher`
   wins when both are present.

3. **Login per account, group items by `as_account`** (v0.3.0+, only when
   `.proctor/config.yml.auth` is set). For each distinct `as_account` value
   in the plan (default = `auth.accounts[0].name` for items without
   one):

   a. Look up the account by name in `auth.accounts`.
   b. Read `email_env`, `password_env`, `totp_seed_env` — each is the
      name of an env var, NOT the credential itself. Fail loudly with
      `aborted: "auth-misconfigured"` if any of those env vars is unset.
   c. Open a fresh Chrome incognito context (chrome-devtools MCP). New
      context per group — cookies from the previous account must not
      leak.
   d. Drive `auth.login_url`:
      - Fill `selectors.email` with `$<email_env>`.
      - Fill `selectors.password` with `$<password_env>`.
      - Click `selectors.submit`.
      - If a TOTP page renders next (heuristic: current URL contains
        `totp` / `2fa`, OR `selectors.totp` exists in DOM), compute
        the 6-digit code with:

        ```bash
        python3 ${CLAUDE_PLUGIN_ROOT}/scripts/totp.py "$<totp_seed_env>"
        ```

        Fill `selectors.totp` with that code, click `selectors.submit`.
      - Verify the resulting URL is NOT `auth.login_url` / a 2fa page —
        if it still is, login failed; emit `aborted: "auth-failed"`
        with the URL as evidence.
   e. Now run all items in this group inside this authed context.
      Items within a group still respect `depends_on` and run in
      parallel where possible; the parallelism limit is per-group
      (3 concurrent) so groups can run concurrently with each other.

   Groups execute concurrently to each other when possible (each
   group has its own browser context). When the run finishes, every
   browser context is torn down.

   For runs WITHOUT `auth:` (legacy mode): skip 3a–d entirely. Items
   dispatch as before, no account grouping.

#### Working directory contract (v0.7.0+, mandatory)

**All inline lint/bash items run from the CONSUMER REPO ROOT** (the dir from which `/proctor:proctor` was invoked). Use the worktree-relative path explicitly:

```bash
WT=".proctor/runs/$RUN_ID/pr-checkout"   # relative to consumer repo root
grep -nE 'pattern' "$WT/path/to/file.go"  # works
```

Do NOT cd into the worktree:

```bash
cd "$WT" && grep -nE 'pattern' path/to/file.go  # breaks log paths
cd "$WT" && grep -nE 'pattern' "$WT/file.go"    # double-prefix
```

The latter is the v0.6.9 e2e failure mode (PR #1126 run `pr1126-75eea89-b7a2689b`) — `cd` into worktree, then references to `$LOGS` (anchored at consumer repo root) resolve relative to the new cwd and break. Bash failed 6+ times with `ugrep: warning: ... No such file or directory` until the orchestrator switched to absolute-paths-from-consumer-root.

Exception: the dev server's `go run` / `pnpm dev` MUST run from inside the worktree (so it compiles PR-head code, not main). For server startup specifically, cd into worktree, source dev_env, exec `go run . &`, then return to consumer repo root for everything else.

4. For each item (within its group's authed context):
   - **Check `data_from` first** (v0.3.23+). If the item declares
     `data_from: ["t-007", ...]` and ANY listed source has status
     `"fail"` or `"skipped"` in the already-accumulated results, do
     NOT dispatch — record this item as:
     ```json
     {"id": "t-008", "status": "skipped",
      "reason": "data-dep-failed: t-007",
      "evidence": "Skipped because upstream t-007 had status=<fail|skipped>; this item's state would have been invalid."}
     ```
     and continue to the next item. This propagates: if t-007 was
     skipped due to t-006, and t-008 has data_from=[t-007], t-008 also
     gets skipped with `data-dep-failed: t-007` (the chain reason is
     visible up one level; the report walks the full chain).
   - **Substitute `{{<id>.<key>}}` templates** (v0.3.25+) in the
     item's `how:`, `preconditions`, AND `verify_precondition_via`
     BEFORE dispatch. The schema guarantees every template references
     a `data_from` source that declares the key in `produces` — but
     the source's actual `outputs` map might be missing the key at
     runtime (e.g. the subagent passed but forgot to capture).
     Substitution rules:
     - Look up `run_context[<id>][<key>]` (where `run_context` is the
       accumulator `{item_id: outputs_dict}` you've been building
       from prior `outputs` fields).
     - If found, replace the template inline with that value.
     - If missing, do NOT dispatch — record this item as:
       ```json
       {"id": "t-008", "status": "skipped",
        "reason": "data-template-missing: t-007.created_id",
        "evidence": "Upstream t-007 reported status=pass but did not return outputs[\"created_id\"]; cannot render template {{t-007.created_id}} in this item's how:."}
       ```
       This catches subagent errors (producer claimed success but
       failed to capture) BEFORE the dependent test runs against
       broken state. Continue to next item.
   - **Run `verify_precondition_via`** (v0.3.29+) if set. Execute the
     (already-substituted) command via Bash inside the group's authed
     context. Treat exit code as the only signal:
     - **Exit 0** → environment matches the precondition; proceed to
       dispatch.
     - **Non-zero exit** → environment does NOT match. Record this
       item as:
       ```json
       {"id": "t-008", "status": "skipped",
        "reason": "precondition-not-met",
        "evidence": "verify_precondition_via `<command>` exited <code>; stderr: <first 200 chars>"}
       ```
       and continue to the next item. Do NOT dispatch the subagent —
       the test would either fail for the wrong reason (looking like
       a regression when it's an environment gap) or pass vacuously
       (the assertion path runs against absent state).
     - **Command not found / shell error** → also skipped with
       `reason: "precondition-not-met"` and a clear stderr in
       evidence; do NOT promote this to a fail.

     The distinction `precondition-not-met` vs `fail` is the whole
     point: it tells the reviewer "this is an environment gap, not a
     bug in the diff under test". Pair with `data-dep-failed` (the
     intra-run sibling) so the reporter can render both as
     non-actionable-skip variants distinct from opt-out skip.
   - **Dispatch policy (v0.7.1+).** Subagents (pr-test-executor) handle ONLY `lint-only` and `bash`/`curl` items. `chrome-devtools` items run **inline in this skill's host session**, against the single chrome-devtools-mcp session that you already logged in for this account group. Don't dispatch chrome-devtools items to subagents — chrome-devtools-mcp uses a single shared profile and concurrent subagent sessions fail with "Use --isolated to run multiple browser instances" (v0.7.0 e2e regression, PR #1126 run `pr1126-75eea89-353a49f0`: 6 chrome subagents tried to spawn in parallel, all hit the lock, executor spent ~2min killing them off + recovering before falling back to inline-in-skill). The inline-in-skill chrome path also makes the screenshot capture trivially correct (the host session writes PNG → `<run-dir>/screenshots/<id>__N__<label>.png` directly via `mcp__chrome-devtools__take_screenshot`), avoiding the v0.6.9 "subagent returns fake screenshot_ref in logs/" failure mode entirely.
   - For non-chrome items: dispatch a fresh subagent with the item JSON (with
     templates already substituted), env, and the path to
     `<logs_dir>/<id>.log`. Items without `as_account` set go into the default
     group (= `auth.accounts[0].name`).
   - For chrome-devtools items: drive the page inline using
     `mcp__chrome-devtools__navigate_page`, `take_snapshot`, `click`, `fill`,
     `evaluate_script`, and call `take_screenshot` AT LEAST the per-item-type
     minimum from `validate_screenshots_contract.py` (render-check ≥1,
     negative ≥1, happy-save ≥2, round-trip ≥2, edit-and-switch ≥3). Each
     screenshot path: `<run-dir>/screenshots/<id>__<N>__<short-label>.png`.
     Record the assembled result item directly (no subagent round-trip).
   - **Validate producer outputs** (v0.3.25+) on the returned result.
     If the original item declared `produces: ["a", "b"]`, the
     subagent's `outputs` dict MUST contain both keys with non-empty
     string values. If any declared key is missing or empty:
     ```json
     {"id": "t-007", "status": "fail",
      "reason": "producer-missing-output",
      "evidence": "Item declared produces=[\"created_id\"] but subagent returned outputs=<missing/empty for that key>. Cannot satisfy downstream data dependency."}
     ```
     (Override whatever status the subagent returned — a producer
     that misses a contract is a fail regardless.)
   - Append to results array. If status is `pass` AND `outputs` is
     non-empty, write `outputs` into `run_context[<this_id>]` so
     downstream items can substitute against it.
   - **Empirical-grounding check** (v0.6.2+). After receiving each
     subagent result, pipe it through:

     ```bash
     echo "$RESULT_JSON" | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/validate_item_result.py
     ```

     If stdout is non-empty, the result claims `precondition-not-met` /
     `environment` without empirical markers (no captured exit code /
     HTTP status / stderr / DOM snapshot, no `command:` field). Append
     the validator's warning to the run's evidence chain — the reporter
     surfaces it visibly. Do NOT override the status; the subagent's
     classification stands. The warning's purpose is reviewer
     visibility so the gap doesn't hide.

     This catches the v0.6.1 failure mode: main AI inline-executed
     items, carried session memory about a prior chacha20poly1305
     error, and skipped 3 happy-save items without re-trying after
     the user fixed the env. Per-item subagent dispatch eliminates
     the session-memory leak structurally (each subagent has fresh
     context). The validator catches it even when dispatch is bypassed.

   - **Verify artifact-capture contract** (v0.4.6+). The
     pr-test-executor agent's contract REQUIRES:
     - `logs_ref` set (the path to `<logs_dir>/<id>.log`) for ALL items.
     - `screenshot_ref` set (the path to `<logs_dir>/screenshots/<id>.png`) for chrome-devtools items.

     Real runs show the subagent skipping these. After receiving the
     result, check both:

     ```bash
     # Missing logs_ref → append warning to evidence
     [ -z "$RESULT_LOGS_REF" ] && \
         echo "WARN: executor returned no logs_ref for <id>; report will render '(not captured)'"
     # Missing screenshot_ref on chrome-devtools → louder warning
     [ "$TOOL" = "chrome-devtools" ] && [ -z "$RESULT_SCREENSHOT_REF" ] && \
         echo "WARN: chrome-devtools item <id> has no screenshot_ref — executor skipped take_screenshot (contract violation); report will flag this visibly"
     ```

     Do NOT downgrade the item's status — the test may have passed
     fine, the gap is just visibility. The reporter's
     `render_item_artifacts.py` will surface the missing artifact in
     the per-item section so the human reviewer sees what's absent.

5. After all items finish, **clean up**: close all browser contexts.

   **Dev-environment teardown** (precedence mirrors step 2b):

   - **`dev_launcher.stop`** (v0.7.11+) — when set, run the command
     via Bash regardless of test pass/fail. Don't fail the run on
     a non-zero exit; surface the rc in the run log and continue.
   - **Legacy `setup:` mode** — kill setup processes by sending
     SIGTERM to the process group started by setup commands (use
     `setsid` to start them; track the PIDs in
     `<logs_dir>/setup.pids`). Preserved for v0.7.10-and-earlier
     consumers.

   Then **tear down the worktree** (v0.3.37+) regardless of pass/fail:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree.py teardown \
       --run-dir .proctor/runs/<run-id>
   ```

   The helper is best-effort: if the worktree has untracked files or
   a process still holds a file, `git worktree remove --force` will
   fail and the marker file `worktree-path.txt` stays for the dev to
   clean up manually. Do NOT fail the run for a teardown error —
   it's not a test result, and the worktree is in the run's own
   directory so it can't accumulate dangerous garbage across runs.
   When the original cwd was already at PR head (no worktree was
   created), this teardown is a no-op.

6. Compute `summary` counters by walking `items`. Validate with
   `schema.py:validate_test_results`. Emit one JSON object.

## Constraints

- Emit exactly one JSON object.
- No item runs twice. No item runs in two subagents simultaneously.
- Subagent output that doesn't parse → record as `{status: "fail", reason: "error", evidence: "executor returned non-JSON"}`.

## Detail references

- Subagent contract: `../../agents/pr-test-executor.md`
- Result schema: `../../scripts/schema.py:validate_test_results`

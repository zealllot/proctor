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

2. **Run setup** from the merged config. Each command via Bash. If any
   exits non-zero → abort with `aborted: "setup-failed"`.

   The merged config is `.pr-test.yml` overlaid by `.pr-test.local.yml`
   when the latter exists (the file is gitignored — only developers
   running PRoctor locally will have one). In practice:

   - **CI runs** (deployed test env, `auth:` present, no `setup:` in
     `.pr-test.yml`, no `.pr-test.local.yml`) → setup is empty, skip
     this step. Auth in step 3 logs into the already-deployed server.
   - **Local runs** (developer's `claude /proctor:proctor`, where
     `.pr-test.local.yml` provides `setup:` to bring up the dev
     server) → setup runs, restarts the local server fresh each
     invocation, THEN auth logs in.
   - **Legacy CI bring-up** (v0.2.x consumers, no `auth:`, just
     `setup:` + `base_url`) → setup runs, auth is skipped.

   Setup commands typically include a "kill previous PRoctor-managed
   PID via pidfile" idiom so every invocation gets a fresh server
   without colliding with other PRoctor instances or the dev's own
   processes. The wizard's `.pr-test.local.yml.example` ships this
   pattern by default.

3. **Login per account, group items by `as_account`** (v0.3.0+, only when
   `.pr-test.yml.auth` is set). For each distinct `as_account` value
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
   - Otherwise dispatch a fresh subagent with the item JSON, env, the
     path to `<logs_dir>/<id>.log`, AND the Chrome context handle for
     its group. Items without `as_account` set go into the default
     group (= `auth.accounts[0].name`).
   - Receive its single-result JSON.
   - Append to results array.

5. After all items finish, **clean up**: close all browser contexts;
   in legacy mode, also kill setup processes by sending SIGTERM to
   the process group started by setup commands (use `setsid` to start
   them; track the PIDs in `<logs_dir>/setup.pids`).

6. Compute `summary` counters by walking `items`. Validate with
   `schema.py:validate_test_results`. Emit one JSON object.

## Constraints

- Emit exactly one JSON object.
- No item runs twice. No item runs in two subagents simultaneously.
- Subagent output that doesn't parse → record as `{status: "fail", reason: "error", evidence: "executor returned non-JSON"}`.

## Detail references

- Subagent contract: `../../agents/pr-test-executor.md`
- Result schema: `../../scripts/schema.py:validate_test_results`

---
name: satisfying-form-preconditions
description: How to satisfy form-level preconditions (image upload, dependent-field requirements) before submitting a save flow. Use when a save-flow item is about to skip with reason=precondition-not-met because an UPSTREAM required-field validator fires before the assertion under test can be reached.
---

# Satisfying form-level preconditions

The single failure mode this skill exists to prevent: an executor sees a
required-field error like `Reward Image cannot be blank` on a happy-save
item, decides the field "can't be filled by the headless driver", and
emits `status=skipped, reason=precondition-not-met` for the item AND
every downstream item that `data_from`s it. In the v0.6.5 run on
mcd-website PR #1115 this cascade skipped 9/11 items — the basic
`Image-Validator` (registered before the new `DigitalContent-Validator`
this PR was testing) fired first on every form submit, so none of the
new validator branches were ever reached.

The fix is mechanical: when a save-flow's assertion target is gated
behind an upstream validator on an unrelated field, the executor must
**fill the unrelated field with a value that passes the upstream
validator**, then re-attempt the save. There are two patterns for doing
this; pick the cleanest for the target app.

## Detection: when this skill applies

You're in this situation when ALL of the following hold:

1. The item's `category` is `frontend` and its assertion is on a
   save / submit flow (the item's `what:` contains words like create,
   save, edit, switch, persist, round-trip, validator).
2. Your first attempt returns an HTTP 422 (or stays on the form URL
   with an error chip rendered).
3. The error message names a field that is NOT the assertion target.
   E.g. test asserts on `DigitalContentType` switching; the response
   contains `Reward Image cannot be blank`. The validator name in the
   source code is unrelated to what the test is supposed to exercise.
4. The blocking error message matches one of these regexes:
   - `cannot be blank` (qor `validations.NewError` default message
     format)
   - `is required` (custom validator format)
   - `must be present`
   - `Reward Image cannot be blank` (the literal mcd-website Image-Validator
     message — what triggered this skill's existence)
5. **The test's `how:` does NOT explicitly tell you to leave that
   field empty as part of the test setup.** If `how:` says "submit
   with image blank", you ARE on a negative-image-validator test
   and you must NOT bypass it.

If items 1–5 are all true, you have a precondition gap — apply one
of the patterns below before declaring the item skipped.

## Pattern A: existing-record reuse (preferred — no upload required)

Use this when:
- The form's blocking field is a media / file / asset picker backed by
  a separate admin resource (a "remote data resource" in qor terms).
- That resource has at least one existing record in the database.

The trick: instead of opening the file-upload modal, query the
backing admin resource's index page for an existing record's primary
key, then inject the precise JSON value the picker would have written
to its hidden field after a real upload. The basic Image-Validator
(or analogous validator) only checks "is the hidden field non-empty,
non-`"null"`, non-`"[]"`" — it doesn't verify the referenced record
exists, isn't deleted, or has a valid file. Any well-formed reference
passes.

### Concrete worked example — qor MediaBox / mcd-website

1. **Find the picker's backing URL.** `take_snapshot` or
   `evaluate_script` to read the MediaBox `<label>` element's
   `data-mediabox-url` attribute. For mcd-website's Digital Content
   "Reward Image" field:

   ```js
   document.querySelector('[data-toggle="qor.mediabox"] label[data-mediabox-url]')
     .getAttribute('data-mediabox-url')
   // => "/admin/media_library?filters[SelectedType].Value=image"
   ```

2. **Find an existing record ID.** Fetch the picker URL and grep
   `data-primary-key="(\d+)"` from the response HTML. Pick the
   first hit (highest ID is fine):

   ```js
   const r = await fetch('/admin/media_library?filters[SelectedType].Value=image',
                        {credentials: 'include', headers: {'Accept': 'text/html'}});
   const html = await r.text();
   const id = html.match(/data-primary-key="(\d+)"/)[1];  // e.g. "5"
   ```

   For mcd-website's Digital Download Asset, the picker URL is
   `/admin/digital_download_assets` (no filter param).

3. **Find the field's hidden textarea.** qor MediaBox stores the
   selected file as a JSON-serialized array in a hidden textarea:

   ```js
   document.querySelector('textarea[name="QorResource.Image"]')
   // value before pick: "null"
   ```

   The textarea's `name=` matches the model field name with a
   `QorResource.` prefix.

4. **Inject the minimum JSON the validator accepts.** The basic
   Image-Validator checks `value == "" || value == "null" || value == "[]"`,
   so any JSON array with one object passes. Minimum shape:

   ```js
   const minimal = [{ID: 5, Url: '//placeholder/x.jpg'}];
   document.querySelector('textarea[name="QorResource.Image"]').value
     = JSON.stringify(minimal);
   ```

   For mcd-website the validator at
   `models/mmr_management/mmr_rewards/coupons.go:458-468`
   (`Image-Validator`) only inspects the meta string for emptiness —
   no DB lookup, no S3 round-trip. The
   `DigitalContent-Validator` for the per-type `DigitalDownloadAsset`
   gate at `digital_downloads.go:361-365` is the same shape — pass
   the same JSON injection technique against
   `textarea[name="QorResource.DigitalDownloadAsset"]`.

5. **Submit the form.** Use `FormData` from the existing `<form>`
   element to preserve hidden CSRF / state, then `fetch` POST:

   ```js
   const form = document.querySelector('form[action*="<resource>"]');
   const fd = new FormData(form);
   fd.set('QorResource.Image', JSON.stringify([{ID:5, Url:'//x/y'}]));
   fd.set('QorResource.Title', 'ai-test-...-tNNN');
   // ... other required fields per the validator
   const resp = await fetch(form.action, {method:'POST', body: fd,
                                          credentials:'include', redirect:'follow'});
   // resp.status === 200 + resp.url ending in /<id> → save succeeded
   // resp.status === 422 + same URL → validator rejected (read text for which)
   ```

6. **Verify the save worked.** Check `resp.url` against
   `form.action` — successful save redirects to `<action>/<new-id>`
   (e.g. `/admin/digital_content/1296`). Capture the ID as the
   produced value if the item declared `produces: ["created_id"]`.

### Generalizing to non-qor apps

If you're not on qor admin, the picker is probably a different shape
but the principle is the same:

| App pattern | Where the picker stores its value | How to bypass |
|---|---|---|
| qor MediaBox | `<textarea>` JSON array | inject array literal |
| ActiveAdmin / Rails has_one_attached | hidden `<input>` with signed blob ID | reuse an existing blob ID from the index |
| Django admin ImageField | `<input type=file>` + on-submit upload | uploads ARE real; use Pattern B |
| Custom React admin | hidden `<input>` with backend file ID | trace the React store → existing record |

If you genuinely can't bypass (no existing records, no JSON store —
the field requires a literal file blob POST), fall back to
Pattern B.

## Pattern B: real upload via the modal

Use this when:
- Pattern A doesn't apply (custom widget, no DB-level shared
  references, file blob required at validation time).
- The deploy env's S3 / storage backend accepts new test uploads.

1. **Open the modal.** Find the "Add" button via snapshot, click it.
   Wait for the modal's file input to be in DOM.
2. **Upload a 1×1 PNG via `upload_file`.** Use a stable test artifact;
   filename convention: `proctor-e2e-stub-<timestamp>.png` so the
   uploaded record is obviously test debris.
3. **Wait for upload completion.** After upload the modal usually
   renders a thumbnail and inserts the picker's hidden field
   automatically. Poll for `textarea[name="QorResource.<Field>"].value`
   to change from `"null"` to a JSON array.
4. **Dismiss the modal** (Escape, or the modal's Close button) and
   continue with form-fill.

If step 2 returns an S3 / storage error (403, network-blocked,
CSRF rejected), pivot back to Pattern A — assume the deploy env's
storage doesn't accept new test uploads from automation and an
existing-record reference is the only feasible option.

## Negative-test screenshot: error must be IN THE DOM, not just response body

This section exists because the v0.6.6 mcd-website run on PR #1115
shipped t-007 / t-008 / t-009 with three byte-identical screenshots
(244252 bytes each, the blank "Add Digital Content" form). Each
item's evidence claimed an error chip rendered, and the HTTP response
body actually DID contain the error HTML — but the screenshots
captured the pre-submit form, not the rendered error. The user
spotted it instantly: three "validator rejected" screenshots showing
zero errors.

**Root cause:** the Pattern A submit step uses
`fetch(form.action, ...)`. `fetch` is a programmatic POST — the
server returns the 422 + error HTML, the executor reads `await
resp.text()` and sees the expected error string (empirical evidence
the validator branch fired), but the **browser DOM never updates**
because `fetch` is decoupled from page navigation. `take_screenshot`
then captures whatever was on screen — which is the pre-submit form.

For **happy-save** items this is fine — the evidence is the redirect
URL + the created ID, and the post-save detail page is reached by
follow-up navigation. For **negative items** it is wrong: the
asserted artifact IS the rendered error, and a fetch-only submit
never renders one.

### Negative-test submit procedure

1. **Apply MediaBox bypass** (or whatever Pattern A injection the
   upstream validator needs — same as the happy-save case).

2. **Set the field under test to the invalid value via DOM**:

   ```js
   document.querySelector('select[name="QorResource.DigitalContentType"]').value = "";
   // or: select.value = "Game"; input[name="QorResource.GameUrl"].value = "";
   // or: input[name="QorResource.GameUrl"].value = "not-a-url";
   ```

3. **Submit via `form.submit()` — NOT `fetch(form.action, ...)`.**
   `form.submit()` is a real browser navigation: the browser POSTs
   the form, the server returns the same 422 + error HTML, and the
   browser RENDERS that response into the DOM. The page URL stays
   the same (qor admin re-renders the form on validation reject
   without changing the URL), so the visual cue is the appearance
   of error chips, not a URL change.

   ```js
   document.querySelector('form[action*="digital_content"]').submit();
   ```

   If `form.submit()` is blocked by CSP / framework JS-interception,
   click the actual submit button via `mcp__chrome-devtools__click`
   on `<input type="submit">` or `<button type="submit">` — that's
   the real-user submit path.

4. **Wait for the error to appear in DOM** via
   `mcp__chrome-devtools__wait_for` on the expected error text or a
   reliable selector:

   ```
   wait_for text=["Digital Content Type is required"]
   ```

   Don't screenshot too early — the error chip is inside the
   re-rendered form section that only exists after the response
   loads. If your `wait_for` times out, the submit didn't actually
   render the error — debug the submit step before screenshotting.

5. **Scroll the error into view** (the v0.6.4 contract) via
   `evaluate_script`. The error chip is often near the affected
   field, not at the page top:

   ```js
   const err = document.querySelector('.qor-error') ||
               [...document.querySelectorAll('*')].find(
                   e => e.innerText && e.innerText.includes('is required'));
   if (err) err.scrollIntoView({block: 'center', behavior: 'instant'});
   ```

6. **Verify the error is visible in the viewport BEFORE
   take_screenshot.** Grep `document.body.innerText` for the
   expected error string:

   ```js
   const expected = 'Digital Content Type is required';
   const present = document.body.innerText.includes(expected);
   // → present must be true; if false, loop step 4-5 (timing).
   ```

   If grep fails, the screenshot timing is wrong. Loop step 4-5;
   don't fall through to take_screenshot — that's how three identical
   pre-submit form PNGs end up in the report.

7. **Take a viewport-cropped screenshot.** The evidence string MUST
   say the error chip rendered **in PAGE DOM** (not "in response
   body"), and SHOULD quote the actual visible error text from
   `document.body.innerText`. Example:

   > "Server returned HTTP 422; error chip 'Digital Content Type is
   > required' visible in page DOM (verified via
   > document.body.innerText grep) at the time of take_screenshot.
   > URL bar remains at /admin/digital_content."

### What NOT to do (negative-test specific)

- **Don't submit via `fetch()` and screenshot the pre-submit form.**
  That's the literal v0.6.6 t-007/008/009 bug — three identical
  PNGs in the report, three evidence strings claiming an error
  rendered. `fetch()` reads the response body but doesn't render it.
- **Don't `take_snapshot` instead of waiting for the error to
  render.** `take_snapshot` captures the form-load state — by the
  time the snapshot returns, the submit may not have completed and
  the error chip may not be in DOM yet. Use `wait_for` keyed on the
  expected error text, then snapshot.
- **Don't take the screenshot BEFORE verifying the error text is in
  the rendered DOM.** A passing `take_snapshot` that lists no error
  chip means the submit hasn't fired; a passing
  `document.body.innerText.includes(expected)` is the only reliable
  signal the screenshot will capture what evidence claims.

## What NOT to do

- **Don't skip with `precondition-not-met` after one attempt that hit
  the upstream validator.** The empirical-grounding rule
  (pr-test-executor.md "NO preemptive skip") applies, AND there's
  a documented technique to satisfy the precondition — use it.
- **Don't fill the blocking field with empty / placeholder strings
  hoping the validator is lenient** (e.g. `' '`, `'fake'`). The qor
  Image-Validator explicitly rejects `""`, `"null"`, `"[]"`. Inject
  a structurally valid value.
- **Don't conclude the upstream validator is a bug.** It's almost
  always intentional — the test's `how:` simply didn't enumerate
  every required field. If `how:` is silent on a required field,
  the planning gap is on the planner side, not the validator.
- **Don't reference non-existent records.** Some validators DO verify
  the referenced primary key exists (e.g. uniqueness checks, FK
  cascade). Always grep the index page first; don't fabricate IDs
  like `ID: 99999`.

## Where this maps in the executor procedure

In `pr-test-executor.md` section 2a ("For chrome-devtools items that
submit a form ... read the validator source first"), step 1 enumerates
every required-field rule. **When that enumeration produces a
required field NOT mentioned in the item's `how:`, AND the field is
gated behind a picker / modal / asset reference** — that's a
precondition gap. Apply this skill's Pattern A (or B) before clicking
Save.

The result evidence should explicitly call out the bypass technique:

> Save succeeded after injecting media_library record ID=5 via
> `textarea[name="QorResource.Image"]` to satisfy the Image-Validator
> (registered before the new DigitalContent-Validator). The asserted
> behavior — DigitalContent-Validator's Game branch passing on a valid
> GameUrl — was then exercised cleanly.

This way the human reviewer sees both the assertion was real AND the
precondition technique used, so they can spot-check both.

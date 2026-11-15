# Sign-in Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace oauth2-proxy's stock sign-in and error pages with the frozen PubMed Ai design (Mock Board `sign-in-page` v2, frozen 15 Nov).

**Architecture:** oauth2-proxy v7.15.6 loads `sign_in.html` and `error.html` from `--custom-templates-dir` (checked in its source: `pkg/app/pagewriter/templates.go`, `addTemplate`; it falls back to the stock page if a file is missing). We keep two hand-written source templates plus a small build script that inlines the fonts and Google's official button, and writes the two finished pages to `gate/templates/`. The finished pages are committed, mounted read-only into the gate, and are fully self-contained, because nothing behind the gate (our `/static`) can load before sign-in.

**Tech Stack:** Go html/template (oauth2-proxy's engine), Python 3 build script, pytest, Go stdlib for a render check (Go is on the build machine).

**Spec:** Mock Board screen `sign-in-page` v2 (frozen). Google sign-in branding rules (light: white fill, #747775 stroke; dark: #131314 fill, #8E918F stroke; standard colour G; never redrawn).

## Global Constraints

- No JavaScript at all on these two pages (the theme follows the phone: `prefers-color-scheme`; no toggle here).
- No external requests: fonts, the badge and the Google button are inlined as data URIs. No link to `/static`.
- The Google button is Google's file, byte for byte: `Theme=Light|Dark, Show text=Yes, Shape=Pill, Platform=Android+Web.svg` from the owner's `signin-assets.zip`. Stored in `gate/assets/google/` with Google's file names shortened to `signin-light.svg` and `signin-dark.svg`. Shown at 234×52 (aspect kept).
- Colours and fonts as the style kit (day #F7F4EC / #FFFFFF / #E4DED0 / #101A24 / #5B6773 / #1F6BD0; night #101A24 / #16222E / #22303E / #EEF2F6 / #93A0AD / #5AA0EE). Weights 300/400/500 only.
- No question text, emails or names on the page. No server names, IPs or domains in the files (the public repo rule). The footer says "Part of SurgeryAi · Educational tool only".
- Keep oauth2-proxy's form contract: GET `{{.ProxyPrefix}}/start` with hidden `rd={{.Redirect}}`.
- Gate stays at v7.15.6. No skip-auth rule.

## Review Focus

1. **A refused account vs other 403s.** Not-on-list gives 403 with oauth2-proxy's default message "You do not have permission to access this resource."; a CSRF/state failure gives 403 with a "Login Failed: …" message. Only the first shows "This account can't sign in"; the others show the general "Sign-in didn't work" card with a Try again button.
2. **Template parse failure.** A bad template makes oauth2-proxy refuse to start (it does not fall back on a parse error). The Go render check must run in tests, so a broken page never reaches main.
3. **Escaping.** `.Message` and `.Redirect` come from the request; they must be printed only through html/template's normal escaping (no `safeHTML`-style tricks), and `rd` must stay inside a hidden input value.
4. **Mount missing on the server.** If the folder is not mounted, oauth2-proxy silently shows the stock page. The compose test pins the volume and the flag.
5. **Size and staleness.** The committed pages must equal what the build script makes from the sources (a test rebuilds and compares), and each page stays under 300 KB.

---

### Task 1: Pages, assets and build script

**Files:**
- Create: `gate/assets/google/signin-light.svg`, `gate/assets/google/signin-dark.svg` (copied unchanged from the zip)
- Create: `gate/src/sign_in.html`, `gate/src/error.html` (source templates with `@@FONT_HANKEN@@`, `@@FONT_SERIF@@`, `@@BADGE_DAY@@`, `@@BADGE_NIGHT@@`, `@@GOOGLE_LIGHT@@`, `@@GOOGLE_DARK@@`, `@@LOCK@@` placeholders)
- Create: `scripts/build_gate_pages.py` with `build(src_dir: Path = SRC) -> dict[str, str]` and a `main()` that writes `gate/templates/sign_in.html` and `gate/templates/error.html`
- Create: `gate/templates/sign_in.html`, `gate/templates/error.html` (generated, committed)
- Test: `tests/test_gate_pages.py`

**Interfaces:**
- Produces: `gate/templates/` (mounted in Task 2); `build()` returns `{"sign_in.html": text, "error.html": text}`.

Page content is the frozen mock, as Go templates:
- `sign_in.html`: `{{define "sign_in.html"}}…{{end}}`. Brand row, card (SIGN IN, PubMed Ai, the description), the official button inside `<form method="GET" action="{{.ProxyPrefix}}/start"><input type="hidden" name="rd" value="{{.Redirect}}"><button type="submit" class="gbtn" aria-label="Sign in with Google"><picture>…</picture></button></form>`, the lock note, the footer.
- `error.html`: `{{define "error.html"}}…{{end}}`. Branch `{{if and (eq .StatusCode 403) (eq .Message "You do not have permission to access this resource.")}}` → the frozen "Not on the list yet / This account can't sign in" card with "Use another Google account:" and the official button (form to `{{.ProxyPrefix}}/start` with `rd={{.Redirect}}`; the account chooser makes it a real choice). `{{else}}` → label "Sign-in problem", h1 "Sign-in didn't work", `<p>{{.Message}}</p>`, the official button (same form) and, if set, a small "Reference: {{.RequestID}}". No "Back to SurgeryAi" button (owner, 15 Nov).

Known limit (oauth2-proxy v7.15.6): a few other 403s carry no message of their own and so get the same default text (wrong token audience, session validation failed, email not verified); they also show the "can't sign in" card. Telling them apart needs the gate's debug mode, which we do not turn on.

- [ ] **Step 1: Write the failing tests** in `tests/test_gate_pages.py`:
  - `test_google_buttons_are_googles_files_unchanged`: sha256 of each file in `gate/assets/google/` equals the sha256 of the zip originals (record the two hashes as constants in the test).
  - `test_committed_pages_match_the_build`: `build(Path("gate/src"), tmp)` equals the committed files' text.
  - `test_pages_have_no_script_and_no_outside_requests`: no `<script`, no `http://`/`https://` in `src=`, `href=`, `srcset=` or `url(` (data: only), no `/static/`.
  - `test_sign_in_form_keeps_the_proxy_contract`: contains `action="{{.ProxyPrefix}}/start"` and `name="rd" value="{{.Redirect}}"`, exactly one Google button.
  - `test_error_page_splits_refused_from_other_403s`: contains the exact default-message string above inside the `eq .StatusCode 403` branch.
  - `test_pages_stay_small`: each page < 300_000 bytes.
  - `test_no_private_strings`: no `@`-email pattern, no IPv4 other than 127.0.0.1, no domain names from the public-repo deny list used by the existing privacy tests.
- [ ] **Step 2: Run** `pytest tests/test_gate_pages.py -q` → fails (files missing).
- [ ] **Step 3: Copy the two Google SVGs, write the source templates from the frozen mock's HTML/CSS, write `build_gate_pages.py`** (base64 data URIs; fonts from `src/pubmedgemini/static/fonts/`; badges from `src/pubmedgemini/static/icons/`), run it.
- [ ] **Step 4: Run** the tests → pass.
- [ ] **Step 5: Commit** the listed files by explicit path.

### Task 2: Real render check with Go, and the gate wiring

**Files:**
- Create: `tests/gate_render/main.go` — reads a template dir, parses `sign_in.html` and `error.html` with `html/template` and the same FuncMap as oauth2-proxy (`ToUpper`, `ToLower`), executes each with the same data struct fields as v7.15.6 (`sign_in_page.go`: ProxyPrefix, StatusCode, Title, Redirect, CustomLogin, SignInMessage, ProviderName, LogoData, Footer, Version; `error_page.go`: Title, Message, ProxyPrefix, StatusCode, Redirect, RequestID, Footer, Version), prints JSON `{case: html}`.
- Modify: `compose.yaml` gate: add `--custom-templates-dir=/etc/gate/templates` to `command`, add volume `./gate/templates:/etc/gate/templates:ro`.
- Modify: `tests/test_compose.py`: add `--custom-templates-dir` to `GATE_FLAGS` (after `--redirect-url`); new test that the volume is mounted read-only.
- Test: `tests/test_gate_render.py` (no skip: CI fails on any skipped test. GitHub's ubuntu-latest runner ships Go; if `go` is missing the test fails with a clear message).

- [ ] **Step 1: Write the failing tests**: render cases sign-in; error 403 default message → contains "This account can't sign in"; error 403 "Login Failed: Unable to find a valid CSRF token. Please try again." → contains "Sign-in didn't work" and the message; error 500 → general card; a `Redirect` of `/x"><script>` → appears escaped (`&#34;` and no `<script>`); a `Message` with `<b>` → escaped. Compose tests for the flag and volume.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Write `main.go`, edit compose.yaml.**
- [ ] **Step 4: Run** the whole suite: `TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/postgres pytest -q -rf` and `ruff check .` → pass. Screenshot both rendered pages (day and night, 390 px and desktop) and compare with the frozen mock.
- [ ] **Step 5: Commit.**

### Task 3: Independent review, then merge on the owner's word

- [ ] Independent Code Reviewer reads this plan and the branch (not the builder's summary), runs the tests, checks the five Review Focus points and the public-repo rule.
- [ ] Fix findings, re-check.
- [ ] Show the owner screenshots; merge only on "merge it". Watch `ci` and `deploy`. After deploy, the owner opens the site in a private window: the new sign-in page must show (if the stock page shows, the mount is missing).

## Owner decisions (15 Nov)

- Account chooser: **yes**, `--prompt=select_account` added to the gate.
- "Back to SurgeryAi" button: **dropped** (the owner's domain cannot be in the public repo; he chose no button over a server setting).
- Getting the pages to the server: **the deploy copies them.** The workflow copies `gate/templates/{sign_in,error}.html` as `gate-<page>.new`; `infra/deploy.sh` checks both are there, saves the live pages with compose.yaml (before `.env.previous`), installs them (folder 755, files 644: the gate runs as non-root), and recreates the gate (`docker compose up -d --no-deps --force-recreate gate`) only when a page changed. A rollback puts the old pages back (none before: none after) and recreates the gate again. After recovering from a deploy that was killed outright, the gate is always recreated (the files cannot show what it is serving). Before anything changes, each page must begin with its `{{define}}` line and `gate/templates` must be a folder this account owns (review findings, 15 Nov). Tests in `tests/test_deploy.py`.

## Earlier note: account chooser (asked before Task 2)

**Account chooser.** Google often signs a person straight back in with the same account, so "Use another Google account" on the refused page may just loop. Adding `--prompt=select_account` to the gate makes Google always show its account chooser (one extra tap for everyone, every sign-in). Suggested: add it. Without it, the refused page drops the second Google button and says "Sign out of Google in this browser, then try again."

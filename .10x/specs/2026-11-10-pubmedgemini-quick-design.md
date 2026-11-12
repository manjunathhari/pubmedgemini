# pubmedgemini: Quick run on free APIs (design)

10 Nov 2026. Status: **design agreed section by section in chat; this written spec awaits the owner's review.** Nothing is built yet.

Source: `plans/2026-11-10-incisive-free-build-handover.md` (Project "Pubmed Incisive"), changed by the owner's decisions of 10 Nov listed in §2.

## 1. Goal

A second, simpler Incisive that costs ₹0 to run.

- It answers a clinical question the way Incisive's Quick run does: a researcher searches PubMed and Europe PMC, a writer writes a structured answer from the abstracts, and code checks the citations and numbers.
- Every model call goes to Google Gemini's free tier, on the Flash-Lite model.
- The answer page follows the owner's frozen mock (version 3, 12 Nov 2026; §6): cards, a CI chart, out-of-100 grids and arm comparisons, all drawn by code from the stored numbers, with no JavaScript. (Until 12 Nov this line read "plain writing; no dashboards, charts or cards".)
- Incisive (Rust, `manjunathhari/incisive`) is not touched. It is the yardstick for quality.

**Success:** on the four benchmark questions (hernia, achalasia, cholecystitis, appendicitis) the owner can read a full answer at ₹0 and judge the gap against Incisive Quick side by side.

## 2. Decisions made by the owner (10 Nov 2026)

| # | Decision |
|---|---|
| 1 | Model: Gemini free tier only. **Flash-Lite does every step**, including the write-up. No Flash, no fallback between models. |
| 2 | Scope: **Quick only.** No Full run. |
| 3 | Pipeline: **the same as Incisive's Quick run** (researcher → study list → writer in four parts → citation check → number check → number completion). |
| 4 | Page: **plain writing in the browser.** No S1e layout, no mockup stage. *Superseded 12 Nov 2026: the owner froze answer-page mock version 3 (§6).* |
| 5 | Language: Python. |
| 6 | Hosting: the app and the database both run on the owner's **VPS** (2 CPU, 8 GB, Ubuntu 24.04 with Docker and Traefik). No Google Cloud. |
| 7 | Repo: `manjunathhari/pubmedgemini`. |
| 8 | Build order: stage by stage (F0–F5, §9), each a small batch the owner signs off. |
| 9 | Google search: none in this build. |

Assumed and not yet contradicted: the owner is the only user at first.

## 3. Limits of the free tier (what we design around)

Figures from a third-party page dated 19 Oct 2026 that cites Google AI Studio. Google's own page could not be opened to confirm them. They are settings in the app, not constants, and are checked in AI Studio at F2.

| Model | Requests a minute | Tokens a minute | Requests a day |
|---|---|---|---|
| Gemini Flash-Lite (free) | 10 | 250,000 | 500 |

- The daily count resets at midnight Pacific time.
- Calls a run (owner decision, 12 Nov 2026, F3 plan D1 option A, Incisive exactly): the researcher at most 11 (8 turns, the study list, its nudge, the S3 retry) and the writer at most 10 (four parts, one retry each, the one-piece fallback and its retry), so at most 21 calls a run and 24 attempts with retries (`gemini/quota.py` `RUN_CALLS`, `RUN_ATTEMPTS`). A question is started only when the day still has room for all 24, so a run admitted with one run left cannot pass the day's 500. "Free runs left" shows 23 on a fresh day; a typical run uses about 13, so about 37 fit (Estimate). F2 was 11, 14 and 45. F4 adds number completion (one call): 22, cap 25.
- **Privacy:** Google may use free-tier prompts and replies, and human reviewers may read them. The page carries a standing line saying so, and asks the user not to type anything that identifies a patient.

## 4. Shape of the system

**Code.** One repo. Python 3.12, FastAPI, Jinja2 templates; no JavaScript (the run page refreshes itself with a `<meta http-equiv="refresh">` while a run works). `httpx` for PubMed and Europe PMC, and `httpx` against Gemini's REST API (`generateContent`, v1beta) for Gemini, not the `google-genai` SDK (corrected 11 Nov 2026 to match the code; reason in the F2 plan's differences table). Pydantic models for the answer. `psycopg` 3 with plain SQL. `pytest` for tests.

**On the VPS.** One Docker Compose project with three services:

| Service | What it does | Reachable from |
|---|---|---|
| `gate` | `oauth2-proxy`: Google sign-in, lets in only the email addresses on the allow list | The internet, through Traefik (HTTPS) |
| `app` | The FastAPI app | `gate` only, on the internal Docker network |
| `db` | Postgres 17 | `app` only. No published port |

- Traefik, already on the VPS, terminates HTTPS and routes the web address to `gate`.
- `gate` is pinned to oauth2-proxy v7.15.6 or later and has no skip-auth rules: every path needs sign-in through it. `/healthz` is called without sign-in only by the app container's own healthcheck, inside the internal network.
- The app reads the signed-in email from the header `gate` sets. It is safe to trust because nothing else can reach `app`.
- A user who passes Google sign-in but is not in the `users` table sees "You haven't been set up yet. Ask the owner to add you."

**Runs.** A run is a background task inside the app process. Each step writes its line and result to Postgres as it finishes; the page polls and reads from there. On start-up the app marks any run still "running" as "stopped". The page shows a "Run again" button on a stopped, failed or partial run (owner decision, 11 Nov 2026: failed and partial too); not on a done, queued or running one.

**Limits.** Two limiters shared by the whole app:
- PubMed: 3 requests a second without a key, 10 with a free NCBI key. Every request carries the registered `tool` value, and the `email` value when one is configured (`NCBI_EMAIL`, optional).
- Gemini: per model, per minute and per day. The day count is kept in Postgres (`model_calls`), so the app refuses to start a run it cannot finish instead of failing halfway. The page shows "Free runs left today: about N".

**Secrets.** The Gemini key, the NCBI key, the Google sign-in client secret, the gate's cookie secret and the database password live in one `.env` file on the VPS, owned by root with mode 600. They are never in the repo, in images or in logs. This stands in for Incisive's Secret Manager.

**Logs.** Question text, search queries, abstracts and model replies are never written to logs. Logs hold counts and fixed kinds only, as in Incisive. The question and the answer are stored only in Postgres, so the owner can reopen past runs.

**Deploy.** Work happens on branches and pull requests. Nothing is merged without the owner's explicit "merge it". On merge to `main`, GitHub Actions runs the tests, builds the image, pushes it to GitHub's container registry, then connects to the VPS with a deploy-only SSH key and runs `infra/deploy.sh <image>`. That script switches `.env` and `compose.yaml`, then waits until the app is healthy on the new image and the gate is running steadily; if not, or if it is interrupted, it restores both files, brings the previous version back up and fails the deploy. A manual rollback is in `infra/README.md`.

**Backups.** A nightly `pg_dump` on the VPS, keeping the last 7 days.

## 5. One Quick run

The same steps as Incisive Quick (`agent/team.rs`, `agent/researcher.rs`, `agent/writer_v3.rs`, `agent/numbers_complete.rs`).

| Step | Done by | Calls |
|---|---|---|
| 1. **Researcher.** A tool-using loop of up to 8 rounds with four tools: `search_pubmed`, `search_europepmc`, `get_abstracts`, `find_related`. Abstracts only | Flash-Lite | 3–8 |
| 2. **Study list.** One structured call over the transcript: up to 10 `selected` (PMID, design, why), plus `considered_not_selected` and `possible_contradictions` | Flash-Lite | 1 |
| 3. **Writer.** Four parts at once over the same input: summary (at a glance, bottom line, clinical practice), context (applicability, what would change), trials (trials, conflicting evidence, notable findings), outcomes. Starts are spaced 6 s apart by the 10-a-minute limit | Flash-Lite | 4 (up to 10 with one retry per part and the one-piece fallback with its retry, as Incisive) |
| 4. **Citation check.** Every citation must be one of the selected studies | Code | 0 |
| 5. **Number check.** Each number in the answer is traced to the abstract text; the rest are marked "not traced". Nothing is removed | Code | 0 |
| 6. **Number completion.** One call to fill untraced numbers from the reference | Flash-Lite | 1 |

Rules carried over from Incisive, unchanged:

- **Study list is untrusted.** Code keeps only PMIDs that appeared in a tool result the model actually saw in this run, removes duplicates and cuts the list to 10.
- **Empty study list (S3).** If `selected` and `considered_not_selected` are both empty while the searches returned papers, the call is made once more, with the visible line "Researcher: the study list came back empty; selecting again". A second empty list ends the run with "No studies could be selected from the papers found. Please run the question again."
- **Search trail.** The searches shown to the user are recorded by code as the tools run, not taken from the model.
- **References are built by code** from the selected PMIDs. The model never writes an author or year citation.
- **Flag, never delete.** Checks mark problems. They do not remove content.
- **No check may fail a run.**
- **Early summary.** The summary part is shown as soon as it has been read and checked, before the other parts finish. (Owner decision 12 Nov 2026, F3 plan D6: built in F4, where the checks are.)
- **One-piece fallback.** If a writer part fails twice, the answer is written in one call instead.
- **Time.** As Incisive Quick: a time limit of 6 minutes (360 s); the research wraps up at 210 s (360 − 150, the writer's allowance) and the run is stopped hard at 420 s (360 + 60), ending "failed" with "This run ran out of time (7 minutes)." (corrected 11 Nov 2026 to match the code). The writer is not started with less than 150 seconds left; the run then ends "partial" with the references.

**Prompts.** The researcher, study-list, writer and number-completion prompts are copied from Incisive. The only edits are the ones Gemini's tool-calling format requires. Any other prompt change is an engine change and is discussed with the owner first.

**Owner-approved differences from Incisive's prompts:**

- **Researcher search checklist** (owner approved 11 Nov 2026, "ok"). One block, the owner's wording word for word, is added to the researcher's system prompt between METHOD step 5 and SELECTION (`researcher/prompts.py` `SEARCH_CHECKLIST`): before it stops, the researcher checks it has run a broad trial search without outcome words, the same limited to the last five years, a five-year search for systematic reviews and meta-analyses, at least one Europe PMC search, a search for each key trial's name, acronym or registration number (selecting the newest follow-up as well as the primary report), and `find_related` on the two most important trials; a search with fewer than 40 results is re-run with fewer terms; a guideline is selected only when the question asks about practice recommendations. The five years are this year and the five before it in UTC, filled by code from the run's date (`from_year:to_year[dp]`, 2021:2026 in 2026). Incisive's words are unchanged around it; the study-list prompt, tool texts, turn and call caps are unchanged. Why: in the four real runs of 11 Nov the model searched too little (4 to 6 searches, narrow queries, Europe PMC in one run of four, older trials), and on the hernia question Incisive Quick with the same prompts found 10 studies against our 7, 3 of them in common.

**Owner decisions for F3 (12 Nov 2026, every suggested option of the F3 plan):** D1 Incisive's call budget (§3); D2 the four parts on four threads, their starts spaced by the pacer, in whatever order the threads reach it (no ordering gate; Incisive starts all four at once); D3 a number is drawn only when the app's simple checks pass (its study is in the list, the numbers are finite, a ratio is above 0, low ≤ estimate ≤ high, percentages 0 to 100), else shown as text with the reason; the log scale runs from the data, never past Incisive's 0.05 to 50; D4 a citation to a study outside the list is shown as "[not in the study list]"; D5 the writer's other fields are shown (§6); D6 early summary in F4; D7 Gemini's default field order; D8 Incisive's prompt words kept; D9 the run fails when the selected studies' records cannot be fetched (Incisive); D10 a wait that would pass the hard stop during the writer ends the run partial; D11 out of 100 for two-arm outcomes only. A reply Gemini blocks is a failed call, not read and not retried (Incisive), recorded with the error kind `refused`.

**On every answer** (bottom of the page, the frozen mock's words; corrected 12 Nov 2026): "Searched PubMed and Europe PMC (free sources); written by <model name> from the abstracts."

## 6. The page

Rewritten 12 Nov 2026 for the answer page the owner froze that day (mock version 3, `docs/mocks/f3-answer-page-v3.html`, kept as history), then version 4 (`docs/mocks/f3-answer-page-v4.html`, 12 Nov 2026: arm names inside the compare rows' values, and long numbers may wrap), now version 5 (`docs/mocks/f3-answer-page-v5.html`, 12 Nov 2026: version 4 with one added rule, `body{overflow-wrap:anywhere}`, so no long text can push the page sideways; owner decision d25, wrapping of long text). Versions 3 and 4 are kept as history. The earlier ruling (10 Nov: "strip the UI, keep the words", a plain list with no bars, no dot grid) is superseded by the frozen mock. Build plan: `docs/superpowers/plans/2026-11-12-f3-writer.md`.

- **Ask page:** a question box, the "Free runs left today" line, the privacy line, and a list of past runs.
- **While a run works:** the run page shows the step lines as plain text, for example "Researcher: 5 searches so far (turn 2 of 8)" and "Writer: writing the answer from 10 studies (10 with an abstract)", and refreshes itself with a meta refresh. When the answer is stored, the run page links to it ("Read the answer"); the run page itself keeps the step lines, the study list and the search trail.
- **Answer page** (`/runs/<id>/answer`), in the mock's order and layout:
  1. **Header:** a link home and to the run page ("Steps and studies"), the question, "Decision support. The clinician decides."
  1a. **At a glance** (when written): the verdict, the key numbers and the main caveat.
  2. **Bottom-line card:** the bottom line, its certainty as a coloured pill with the reason, "Default choice", and "Who may benefit", "Counselling" and "Cost per case" when written. With no written answer: the end message instead, and the references below.
  3. **Why this conclusion:** two cards side by side, **Every estimate with a 95% CI** (left) and **Outcome by outcome** (right).
     - CI card: ratios (RR, OR, HR) first, on one log scale with a dashed mark at 1; then differences, each on its own scale with a dashed mark at 0. Each row: a thin line for the interval, a short bar for the estimate, the scale's end labels and the mark's label. A result without an interval is not in this card. A number that fails the code checks is listed as text with the reason ("Not drawn: …"), never drawn and never hidden.
     - Outcomes card: one closed `<details>` per outcome (all closed): its name, certainty pill, evidence label, number count and first sentence; opened, its evidence, effect text and "Why this certainty".
  4. **Counselling dashboard:** two cards side by side, **Out of 100 people like those in the trials** (left) and **Compare the arms** (right).
     - Out of 100: every two-arm outcome given in percentages, one under another: a sentence ("Chronic pain at 5 years: 9.4% with TEP vs 18.8% with Lichtenstein [4]. Certainty for this outcome: Moderate."), then each arm's count of 100 (rounded half up, as Incisive) with a 10 × 10 grid of squares.
     - Compare the arms: the arm-by-arm results grouped by each study's comparison, each with its certainty, values, study and p. Each value follows the arm name the writer gave for that number ("TEP 9.4% vs Lichtenstein 18.8%", as Incisive's page); a value with no arm name is not shown as a pair but listed in its outcome with "Not drawn: arm not named." (owner decision d23, 12 Nov 2026).
  4a. **Conflicting evidence**, **Applicability** (case mix, India and cost, training) and **What would change this answer**, each only when written.
  5. **The trials:** one entry per trial with its reference number, label, design, size, setting, comparison, key results and limitations.
  5a. **Other notable findings** (the writer's, then the code's merge notes) and the **Search trail**, each only when there is any.
  6. **References:** numbered by code in the researcher's order, each a link to PubMed.
  7. The model line, "Searched PubMed and Europe PMC (free sources); written by <model name> from the abstracts.", and the privacy line.
- **Layout rules:** two columns from 700 px wide, stacked on phones (16 px side margin, no sideways scrolling, even for long unbroken text); the two cards of a pair are the same height; the longer card's extra items fold behind "Show N more …" (a `<details>`) at its bottom, by a fixed rule in code; light and dark by the system setting. **No JavaScript.**
- **The model draws nothing:** every line, bar, square, pill and fold is computed by code from the stored numbers.
- **Differences from Incisive's page the owner accepted (12 Nov 2026):** no All / Observational filter on the CI card; no Clinician / Patient switch; the out-of-100 outcomes are listed one under another instead of chosen one at a time. Also accepted (owner decision d24, 12 Nov 2026): a number is drawn as a difference in the CI card only when it reads as a difference by Incisive's rule and carries no arm name (an `arm` whose whole text, trimmed and case-folded, is a difference marker — "MD", "SMD", "WMD", "difference", "mean difference", "risk difference", "absolute difference" — counts as none; text inside an arm name, as in "MD Anderson protocol", never makes a difference: re-check H-A); a number with an arm name is one arm's value, listed in its outcome when it has an interval, with "Not drawn: an arm is named on a difference." when its label or unit names a difference measure (MD, mean difference and the like: re-check M-C, coordinator ruling option a) and "Not drawn: one arm's rate, not a difference." otherwise, and taking part in "Out of 100" and "Compare the arms" by the usual rules. Incisive draws such a number as a difference. Also accepted (owner decision d23, 12 Nov 2026): a value is never shown for an arm the writer did not name; a pair where either arm has no name is not drawn in "Out of 100" (no squares, no big number) nor in "Compare the arms", and its numbers are listed in the outcome with "Not drawn: arm not named." Incisive labels such an arm "arm 2" and draws it. Also, under the owner's rule that only what passes the checks is drawn (F3 re-check, 12 Nov 2026; plan X30 and X31, not yet confirmed by the owner one by one): arm names that differ only by case or spacing ("TEP" and "tep") are one arm, so such a group is not a pair and its numbers are listed with "Not drawn: its arms are not distinct." (the writer's own spelling is shown); Incisive compares arm names trimmed only. A number with no study (a blank PMID, or one not in the references) is never grouped or paired with other numbers, since they may come from different studies; it is listed with "Not drawn: its study is not in the references." Incisive groups numbers by label and PMID as written. Also, under the same rule (F3 third check, 12 Nov 2026; plan X32 to X34, following owner decisions d23 and d24): only a plain arm name is paired in "Out of 100" and "Compare the arms"; an arm text that reads as a comparison (it contains the word difference, minus, versus, vs, compared with, compared to or relative to, or it is a measure such as "MD.", "M.D.", "Difference:", ARR, RD, ARD, NNT or NNH) is listed with "Not drawn: its arm text reads as a comparison, not an arm." (so "Drain minus" is now listed, not paired; "MD Anderson protocol" stays an arm). A number is drawn as a difference only when, besides having no arm, it names a difference measure: the unit is MD, SMD, WMD or a named kind of difference; or the label contains "mean difference", "risk difference", "absolute difference" or another named kind; or the label is or starts with "difference", MD, SMD or WMD followed by a separator (",", ":", "(", a dash, " in ", " for ", " of "), MD, SMD and WMD only in capitals and not before a capitalised word; anything else that only mentions a difference ("Recurrence (no significant difference)", "MD Anderson Dysphagia Inventory score", "Robotic minus lap") is listed with "Not drawn: not clearly a difference between arms." Incisive draws all of these. Bidi controls and zero-width characters are removed from the model's text before it is stored and again on the page; Incisive keeps them. Also, under the same rule (F3 fourth check, 12 Nov 2026; plan X35 to X39, coordinator ruling option a): the out-of-100 squares are drawn only when the study's comparison has exactly two sides and the two arm texts match opposite sides by their words; otherwise the pair stays a named text row in "Compare the arms" with "Not drawn: its arms do not match the study's comparison." (or "the study's comparison does not name two sides"); arm texts with operator punctuation (a spaced dash or minus, "/", ">", "<", "v"), the delta sign or summary words (diff, delta, reduction, increase, change, ARR, RRR, ARD, NNT, NNH, RR, OR in capitals, HR, relative risk, odds, hazard, ratio, pooled, overall, total, both, all patients, combined) are never paired. A named kind of difference makes a difference row only at the start of the label, as the part after its last comma, as the whole unit or as a whole arm marker, and never when the label holds a negation or significance word (no, not, non, without, none, neither, nor, NS, n.s., nonsignificant, insignificant). A pair whose label names a difference gets no squares ("Not drawn: the label names a difference, not a rate."), nor does one whose label names a change rather than a rate by a whole word, case-folded: reduction, reduced, increase, increased, decrease, decreased, change, changed, improvement, gain, excess, delta, Δ, relative risk, risk ratio, odds ratio, hazard ratio, ratio, NNT, NNH, ARR, RRR ("Not drawn: the label names a change, not a rate."; coordinator ruling, plan X40); a ratio whose label names a difference, and any interval whose two ends are equal, are listed ("the label names a difference but the unit is a ratio", "the interval has no width"); a difference whose label names one arm of its study is listed ("the label names one arm"). Incisive draws all of these.
- **Decided 12 Nov 2026** (F3 plan D3, D4, D5): the writer's other fields are placed as 1a, 2, 4a and 5a above; a number is drawn only when the code checks pass (§5); a citation outside the study list reads "[not in the study list]" and nothing is removed. The mock fixes the layout and the drawing, not the number of rows: where Incisive's rules give more rows than the mock's example (more out-of-100 items, more comparisons), they are all shown and the fold keeps the pair level. With no structured numbers at all, Incisive's note replaces the CI card; a pair with one empty card shows the other alone at full width.
- **Not carried over:** the chat button, the introduction panel, the progress panel's styling, the CI filter, tabs, pop-up study cards and "Copy as text".
- **Untraced numbers** (F4) carry the words "not traced" beside them.

## 7. What is stored

Tables as built (F0 to F2; brought in line with the migrations on 11 Nov 2026):

| Table | Holds | Migration |
|---|---|---|
| `schema_migrations` | The name of each migration file applied | made by `migrate.py` |
| `users` | Email addresses set up to use the app, with a role | `0001_users.sql` |
| `papers` | Each paper once: PMID, PMCID, DOI, title, authors, journal, year, publication types, design, abstract, retracted, when fetched. Served for 30 days, removed after 90 | `0002_papers.sql` |
| `model_calls` | One row per Gemini attempt: model, time, UTC and Pacific day, step, attempt, token counts, latency, a fixed status kind, the run. No text | `0003_model_calls.sql`, `0004_runs.sql` (run) |
| `runs` | One row per question: question text, mode, status, error kind, stop reason, rounds, model, timings. At most one queued or running run for the whole app | `0004_runs.sql` |
| `run_steps` | The step lines shown on the page | `0004_runs.sql` |
| `run_searches` | The search trail, recorded by code as each search returns: source, query, hits | `0004_runs.sql` |
| `run_seen` | The papers the model saw in this run: PMID, title, journal, year | `0004_runs.sql` |
| `run_papers` | Which papers a run selected (with the design label and why), only considered (with the reason) or flagged as a possible contradiction (with the note) | `0004_runs.sql` |

Added in F3:

| Table | Holds | Migration |
|---|---|---|
| `answers` | One row per run that reached its end (done or partial): the answer as JSON in the shape of Incisive's `AnswerV3` (Quick fields only; the question stays in `runs.question`), with code-built references, the integrity counts and, from F4, the number-check results; an empty answer carries its end message. Stored only while the run is still running, so a run a restart stopped has none. Failed runs and runs from before F3 have no row | `0005_answers.sql` |

- Changed from the section shown in chat: there is no `extractions` table, because Incisive Quick has no separate extraction step. The number-check results live inside the answer, as in Incisive.
- Table changes are numbered SQL files in `migrations/`, applied in order at start-up.

## 8. When things go wrong

| Problem | What the app does | What the user sees |
|---|---|---|
| Gemini returns 429 (per-minute limit) | Waits and retries, up to the run's time limit | "Waiting for the free limit (about N s)" |
| Day's allowance used up | Does not start the run | "Daily free limit reached. Resets at <time> IST." |
| Gemini reply is not valid JSON | One retry of that call | Nothing, unless it fails twice |
| A writer part fails twice | Writes the answer in one piece, if at least 150 s are left before the hard stop | "Writer: section <name> failed; writing the answer in one piece" (Incisive's words; corrected 12 Nov 2026) |
| PubMed or Europe PMC is down | The researcher carries on with the other | "Europe PMC could not be reached; searched PubMed only" |
| No papers found | Ends the run "done" | "No relevant evidence found." |
| App restarts mid-run | Marks the run "stopped" | "This run was stopped. Run again?" |
| Out of time before the writer, or a wait for the free limit that would pass the hard stop during it | Ends "partial" (`writer_time`) | "Not enough of the time limit was left to write the answer. The studies found are listed below; please ask again." with the references |
| The writer's answer is cut off, or unreadable, after its retries and the one-piece fallback | Ends "partial" (`writer_truncated`, `writer_unreadable`) | Incisive's message ("The answer was longer than the writer's output limit…" / "The writer's answer could not be read…") with the references |
| The selected studies' records cannot be fetched | Ends "failed" (`lit`); the study list is kept on the run page | "The selected studies' records could not be fetched, so no answer could be written." |
| A writer call fails (Gemini busy after its retries, a refused request, a blocked reply) | Ends "failed" with its kind | "Gemini could not be reached or refused the request. Please try again in a minute." |
| Database down, or start-up work (migrations, owner row) failed | `/healthz` answers 503 `database_unreachable` or `not_ready` (200 `ok` when ready); retries start-up at most every 5 s | "The database could not be reached..." or "The app could not finish starting..." |
| A deploy's new version is not healthy, or the deploy is interrupted | `infra/deploy.sh` restores the previous `.env` and `compose.yaml` and starts the previous version | The previous version; the GitHub deploy shows as failed |

## 9. Stages

Each stage is a small batch on its own branch and pull request, signed off by the owner on a board.

| Stage | What | Done when |
|---|---|---|
| **F0** | Repo skeleton, Postgres, Google sign-in gate, CI, deploy to the VPS | A page loads behind sign-in at the web address |
| **F1** | The four search tools, no model: type a question, see PubMed and Europe PMC results as plain text | Useful on its own at ₹0 |
| **F2** | Gemini layer with limits, the researcher loop and the study list | Picks look sensible on the four benchmark questions |
| **F3** | Writer in four parts and the answer page (frozen mock v5, §6) | A full answer is on the page |
| **F4** | Citation check, number check, number completion, empty-list retry | Matches Incisive's checks |
| **F5** | Side by side with Incisive Quick on the benchmarks: quality and time | The owner judges the gap |

## 10. Testing

- **Unit tests** for everything that is code: study-list validation, citation check, number tracing, reference building, the limiters, the day counter. The citation and number checks are tested against Incisive's own cases, carried over.
- **Recorded replies:** PubMed, Europe PMC and Gemini replies are recorded once and replayed in tests, so tests cost nothing and use none of the daily allowance.
- **Real runs:** the four benchmark questions are run for real at the sign-off of F2, F3, F4 and F5.
- **Logging test:** a test fails if question text or a model reply appears in any log line.

## 11. Known risks

1. **The researcher on Flash-Lite.** A multi-round, tool-using search is the hardest job for a small model. It is tested first, at F2. If it does not hold up, the fallback is a fixed search plan in code with one model call to pick studies; that would be a new decision for the owner.
2. **Writer quality on Flash-Lite.** Expected to be below Incisive. F5 measures it.
3. **Long structured replies.** The outcomes part is a large JSON reply. Flash-Lite's output cap and its reliability at that length are checked at F3.
4. **The free limits change.** Google has cut them this year. The limits are settings, and the page always shows what is left.
5. **One small VPS** shared with Gemma. Enough for one user; not sized for many.

## 12. Not in this build

Full mode, full text of papers, Google search, guideline search on society websites, citation counts (OpenAlex, Semantic Scholar), retraction checks (Crossref), ongoing trials (ClinicalTrials.gov), the doctor's own key, the plugin form, and the interactive parts of Incisive's page (filters, the Clinician/Patient switch, pop-up study cards). Each can be proposed after F5.

## 13. What the owner supplies at F0

1. A web address for the app (for example a subdomain of a domain he owns), pointed at the VPS.
2. The Google accounts allowed in.
3. A Gemini API key from Google AI Studio (free tier).
4. A free NCBI API key.
5. A Google sign-in client (ID and secret) for the gate.

Each is a one-step task, given one at a time when F0 reaches it. Keys are typed on the VPS, never pasted into chat or the repo.

## 14. Working rules (from the Incisive handover)

- One board per goal, with sign-off. Decisions take free text.
- Every engine change is discussed first, however small.
- Small batches. Change only what the owner names.
- Merge only on the owner's explicit "merge it", then watch the deploy.
- When asking for a tag: give the tag name, target commit, release title and description, ready to paste.
- Short replies in plain words. One-line terminal commands, one step at a time.
- Keep a change log and handover in the Project under `plans/`.

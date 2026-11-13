# pubmedgemini: Quick run on free APIs (design)

10 Nov 2026. Status: **design agreed section by section in chat; this written spec awaits the owner's review.** Nothing is built yet.

Source: `plans/2026-11-10-incisive-free-build-handover.md` (Project "Pubmed Incisive"), changed by the owner's decisions of 10 Nov listed in §2.

## 1. Goal

A second, simpler Incisive that costs ₹0 to run.

- It answers a clinical question the way Incisive's Quick run does: a researcher searches PubMed and Europe PMC, a writer writes a structured answer from the abstracts, and code checks the citations and numbers.
- Every model call goes to Google Gemini's free tier, on the Flash-Lite model.
- The answer page has the same layout and sections as Incisive's answer page, in greys (owner decision, 11 Nov 2026; it replaces the plain-writing ruling of 10 Nov. See §2a and §6).
- Incisive (Rust, `manjunathhari/incisive`) is not touched. It is the yardstick for quality.

**Success:** on the four benchmark questions (hernia, achalasia, cholecystitis, appendicitis) the owner can read a full answer at ₹0 and judge the gap against Incisive Quick side by side.

## 2. Decisions made by the owner (10 Nov 2026)

| # | Decision |
|---|---|
| 1 | Model: Gemini free tier only. **Flash-Lite does every step**, including the write-up. No Flash, no fallback between models. |
| 2 | Scope: **Quick only.** No Full run. |
| 3 | Pipeline: **the same as Incisive's Quick run** (researcher → study list → writer in four parts → citation check → number check → number completion). |
| 4 | Page: ~~plain writing in the browser. No S1e layout, no mockup stage.~~ **Replaced on 11 Nov 2026 by §2a no. 1.** |
| 5 | Language: Python. |
| 6 | Hosting: the app and the database both run on the owner's **VPS** (2 CPU, 8 GB, Ubuntu 24.04 with Docker and Traefik). No Google Cloud. |
| 7 | Repo: `manjunathhari/pubmedgemini`. |
| 8 | Build order: stage by stage (F0–F5, §9), each a small batch the owner signs off. |
| 9 | Google search: none in this build. |

The owner was the only user at first. From 11 Nov 2026 the plan is doctors the owner adds himself (§2a no. 5).

## 2a. Decisions made by the owner (11 Nov 2026)

Made in the Project chat of 11 Nov; recorded there in `plans/2026-11-11-pubmedgemini-carry-across.md`. The owner confirmed that nos. 1 and 3 replace his rulings of 10 Nov.

| # | Decision |
|---|---|
| 1 | Answer page: **the same layout and sections as the current Incisive answer page** (the bile leak Quick run of 10 Nov), without Incisive's colours. Replaces §2 no. 4. Mockup first, checked by the owner on his phone. |
| 2 | Look: **greys only.** Text `#333333`, filled boxes `#4D4D4D`, outlines `#8C8C8C`, grey text `#666666`, ground `#F4F4F4`, white cards. Fonts **Hanken Grotesk** (controls, labels, numbers, headings) and **Source Serif 4** (reading text), self-hosted. The owner signed off a mockup of the shell pages on 11 Nov. |
| 3 | The **Introduction** panel is in (folded, written while the search runs). Replaces "not carried over" in §6. |
| 4 | Menu: **Ask, Library, Activity.** No Explore, no "Chat with me". |
| 5 | Users: **doctors the owner adds himself**, by email; Google sign-in; **10 searches a day each** (a setting). |
| 6 | Model: Gemini free tier by default; a doctor's own key as an option, later (still outside this build, §12). |
| 7 | Order of work: the look; then F3 with the answer page to the Incisive layout; then the doctors' part (add/remove page, the 10-a-day count, Library, Activity); then F4 and F5. |

Not yet decided, and needed before no. 5 is built: how the owner's add/remove page and the sign-in gate share the list of allowed emails (the gate reads a file on the VPS today). The whole app also has one free allowance (about 45 runs a day; 21 from F3a, §4) and runs one question at a time; both matter once several doctors use it.

## 3. Limits of the free tier (what we design around)

Figures from a third-party page dated 19 Oct 2026 that cites Google AI Studio. Google's own page could not be opened to confirm them. They are settings in the app, not constants, and are checked in AI Studio at F2.

| Model | Requests a minute | Tokens a minute | Requests a day |
|---|---|---|---|
| Gemini Flash-Lite (free) | 10 | 250,000 | 500 |

- The daily count resets at midnight Pacific time.
- A run makes 13 calls when nothing is retried (8 researcher, 1 study list, 4 writer parts) and at most 26 attempts (§4). The app counts every run after the first at 23, the worst case without transport retries, so the allowance is 21 runs on a fresh day (as built in F3a; 45 until then).
- **Privacy:** Google may use free-tier prompts and replies, and human reviewers may read them. The page carries a standing line saying so, and asks the user not to type anything that identifies a patient.

## 4. Shape of the system

**Code.** One repo. Python 3.12, FastAPI, Jinja2 templates; no JavaScript up to F2 (the run page refreshes itself with a `<meta http-equiv="refresh">` while a run works). No JavaScript in the answer page either: its folds are HTML `<details>` and its charts inline SVG (owner decision N9, 12 Nov 2026, following the frozen mock and the owner's no-JavaScript ruling; replaces the earlier plan of a small script for filters, tabs, folds and a study sheet). `httpx` for PubMed and Europe PMC, and `httpx` against Gemini's REST API (`generateContent`, v1beta) for Gemini, not the `google-genai` SDK (corrected 11 Nov 2026 to match the code; reason in the F2 plan's differences table). Pydantic models for the answer. `psycopg` 3 with plain SQL. `pytest` for tests.

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
- Per run: at most **26 Gemini attempts**, retries included (`RUN_ATTEMPTS` = `RUN_CALLS` 23 + `CALL_RESERVE` 3; was 14 until F3a, owner decision 11 Nov 2026). The 23 is the worst case without transport retries: researcher 8, study list 1, its nudge 1, its empty-list retry 1, writer parts 4, part retries 4, one piece 1 and its retry 1, Introduction 1 (F3c, one attempt, never a retry), number completion 1 (F4). A run that reaches 26 ends `failed` with `run_call_cap`. "Free runs left" is `1 + (rpd - used - 26) // 23`: **21 on a fresh day of 500**, and the same number is enforced when a question starts (owner decision Q4, 11 Nov 2026).

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
| 3. **Writer** (built in F3a). Four parts at once on threads over the same input: summary (at a glance, bottom line, clinical practice), context (applicability, what would change), trials (trials, conflicting evidence, notable findings), outcomes. Incisive's prompts word for word; a part that cannot be read or is cut off gets one retry with Incisive's nudge line; code merges the four, and builds the references | Flash-Lite | 4 (up to 8 with retries; 2 more for the one piece) |
| 4. **Citation check** (moved into F3a, owner decision D4, 11 Nov 2026). Every citation, number and trial row must point to a study that was selected and fetched; those that do not are **removed** and counted, and one line ("Citation check: ...") is shown. As Incisive (`answer_v3.rs`) | Code | 0 |
| 5. **Completeness check** (built in F4a, 13 Nov 2026; owner decisions F4a-1 to F4a-5 and O1 to O6). After the answer is stored and shown, code counts (a) how many of the answer's structured outcome numbers are printed in the abstract of the study each one cites (the same figure, or the abstract's figure rounded to the answer's decimals; each given interval end too) and (b) how many trial names agree with PubMed's record (its year, and its first author's surname or a name printed in the study's own title or abstract). **Counts only:** nothing is marked "not traced" on the page, nothing is named, removed or changed, and no re-run is started or offered. A count means "this figure is printed somewhere in the cited abstract", not "this figure is right" (the rule does not read the unit, the arm or the sign). Numbers inside prose, the participant counts and p values are not checked | Code | 0 |
| 6. **Number completion.** One call to fill untraced numbers from the reference | Flash-Lite | 1 |

Rules carried over from Incisive, unchanged:

- **Study list is untrusted.** Code keeps only PMIDs that appeared in a tool result the model actually saw in this run, removes duplicates and cuts the list to 10.
- **Empty study list (S3).** If `selected` and `considered_not_selected` are both empty while the searches returned papers, the call is made once more, with the visible line "Researcher: the study list came back empty; selecting again". A second empty list ends the run with "No studies could be selected from the papers found. Please run the question again."
- **Search trail.** The searches shown to the user are recorded by code as the tools run, not taken from the model.
- **References are built by code** from the selected PMIDs. The model never writes an author or year citation.
- **Flag, never delete.** Checks mark problems. They do not remove content. **One stated exception (owner decision Q8, 11 Nov 2026):** the citation check removes a citation, number or trial row that points to a study that was not selected or not fetched, as Incisive does. The number check (F4) still only marks.
- **No check may fail a run.**
- **Early summary** (built in F3c, 12 Nov 2026; D8, owner answers O2 and O3). The summary and context parts are shown as soon as each has been read and has passed the same citation check as the final answer, against the same fetched studies, before the other parts finish. The page shows it once the summary part is in, marked "Early", with At a glance's key numbers held back until the full answer (no lines or squares can be drawn: the outcomes part is not in it). It is stored in `run_early` while the run works and deleted when the run ends, whatever the end, in the same transaction; a part that fails for good withdraws it at once; start-up deletes every one.
- **Introduction** (built in F3c, 12 Nov 2026; §2a no. 3, D3, owner answers O1, O4, O5). One call from the question alone (Incisive's `intro_page.rs` prompt word for word, the question as a `<source>`), on its own thread, started as the run starts: one attempt, 30 s, 800 output tokens, counted as one of the run's 26 attempts and in the day's count (step `intro`). Any failure ends its own row `failed` with a fixed reason and the page shows nothing; it never fails, slows (beyond its one pacer slot) or changes the run. It is ungrounded background: the page says so ("Background only, written by the model from your question alone. It is not drawn from the studies and carries no citations.").
- **One-piece fallback.** If a writer part fails twice, the answer is written in one call instead (built in F3a). A blocked reply (`blockReason`, `SAFETY`) counts as unreadable and gets the one retry (Q3).
- **A part hits a limit that cannot be waited out** (the daily limit, a refused key or billing, the run's 26 attempts, the hard stop): the run ends at once, with no one-piece fallback (owner decision Q2, 11 Nov 2026; Incisive tries the one piece). Parts still waiting to retry send nothing more once another has ended the run (a recorded difference from Incisive; cost if wrong: one skipped retry on a run that is ending anyway).
- **Records that cannot be fetched** fail the run (D5), with a plain message (Q6). The answer and the run's final status are stored together.
- **Time.** As Incisive Quick: a time limit of 6 minutes (360 s); the research wraps up at 210 s (360 − 150, the writer's allowance) and the run is stopped hard at 420 s (360 + 60), ending "failed" with "This run ran out of time (7 minutes)." (corrected 11 Nov 2026 to match the code). The writer is not started with less than 150 seconds left; the run then ends "partial" with the references.

**Prompts.** The researcher, study-list, writer and number-completion prompts are copied from Incisive. The only edits are the ones Gemini's tool-calling format requires. Any other prompt change is an engine change and is discussed with the owner first.

**Owner-approved differences from Incisive's prompts:**

- **Researcher search checklist** (owner approved 11 Nov 2026, "ok"). One block, the owner's wording word for word, is added to the researcher's system prompt between METHOD step 5 and SELECTION (`researcher/prompts.py` `SEARCH_CHECKLIST`): before it stops, the researcher checks it has run a broad trial search without outcome words, the same limited to the last five years, a five-year search for systematic reviews and meta-analyses, at least one Europe PMC search, a search for each key trial's name, acronym or registration number (selecting the newest follow-up as well as the primary report), and `find_related` on the two most important trials; a search with fewer than 40 results is re-run with fewer terms; a guideline is selected only when the question asks about practice recommendations. The five years are this year and the five before it in UTC, filled by code from the run's date (`from_year:to_year[dp]`, 2021:2026 in 2026). Incisive's words are unchanged around it; the study-list prompt, tool texts, turn and call caps are unchanged. Why: in the four real runs of 11 Nov the model searched too little (4 to 6 searches, narrow queries, Europe PMC in one run of four, older trials), and on the hernia question Incisive Quick with the same prompts found 10 studies against our 7, 3 of them in common.

**Top of every answer:** "Searched PubMed and Europe PMC (free sources); answer written by <model name>".

## 6. The page

Up to F2: plain HTML (headings, paragraphs, simple lists). Readable on a phone. From 11 Nov 2026 the look is §2a no. 2 and the answer page is the Incisive layout (below).

- **Ask page:** a question box, the "Free runs left today" line, the privacy line, and a list of past runs.
- **While a run works:** the step lines as plain text, for example "Researcher: 5 searches so far (turn 2 of 8)" and "Writer: writing the answer from 10 studies".
- **Answer page as built (F3b, 12 Nov 2026; owner decisions N1 to N11).** The run page itself is the answer page once a run has a stored answer (N1 A); the look is mock v6 (`docs/mocks/f3b-answer-page-v6.html`), the frozen mock v5's layout and drawing in the live greys and fonts. In order: the question as the title with "Done · Quick · Decision support. The clinician decides."; the steps, folded (N8); the outcome line and "Run again" when stopped, failed or partial; At a glance; Bottom line with its certainty label and the four clinical-practice lines; "Why this conclusion" (every estimate with a 95% CI beside outcome by outcome, each outcome folded); "Counselling dashboard" (out of 100, solid against hatched squares, beside compare the arms); then cards for conflicting evidence, applicability, what would change this answer, guidelines, the trials, other notable findings (with the merge notes), search trail and references; then, folded, the selected studies with their abstracts, considered, and possible contradictions; the model line and the privacy line. Certainty labels are greys with the word always printed (N4). From 700 px the page's content is wider (68 rem) and the two cards of a pair sit side by side, level, the longer one folding its extra items behind "Show N more" (N2). Light only (N7). A CI row or a pair is drawn only when it positively qualifies (allow-list); anything else is listed under its outcome or in Compare the arms with the true reason ("Not drawn: ..."). Recovery rules R1, S1 and S2 (N5, N6) are in the F3b plan. Not built here: the results card, the study sheet and "Copy as text". **F3c (12 Nov 2026, mock `docs/mocks/f3c-introduction-summary-v1.html`):** the Introduction folded between the sub line and the steps; while the run works, the Introduction open after the question (with the background line and Incisive's closing line), and once the summary part is in, the Introduction and the steps folded and the early summary cards. Runs without a stored answer show the page as before.
- **Answer page (owner decision, 11 Nov 2026; replaces the plain version below).** The same sections, in the same order, as the Incisive answer page, in greys:
  1. Question as the title, with status and mode labels.
  2. Introduction (folded).
  3. Results card: total time, searches run, studies selected, answer state, citations checked, numbers traced; the progress steps folded beneath.
  4. At a glance: verdict, three number tiles, main caveat.
  5. Bottom line, with the certainty label.
  6. What this means for clinical practice: default choice, who may benefit, counselling, cost.
  7. "Why this conclusion": every estimate with a 95% CI (bars on a scale), beside outcome by outcome (each outcome folds open).
  8. "Counselling dashboard": out of 100 patients (grid, Clinician and Patient views), beside compare the arms (bars).
  9. Details, folded: conflicting evidence, applicability, what would change this answer, the trials (each with study details), other notable findings, references.
  10. A study sheet that opens from any study or citation.
  11. Copy as text, search trail, time.

  Where Incisive uses colour to carry meaning (certainty labels, "not traced", the two arms), this page uses words, weight and pattern: filled or outlined labels, solid against hatched bars. The "Original run view (for checking)" block and the chat tab are left out.
- **Answer page, plain version (the ruling of 10 Nov; kept here as the record and as the content list, since the words and numbers on the page are the same).** Strip the UI, keep the words, and keep the confidence intervals and the counselling without graphics. In order:
  1. **Results line:** total time, searches run, studies selected (RCTs, meta-analyses), citations checked, numbers traced.
  2. **At a glance.**
  3. **Bottom line**, with its certainty and the reasons for it.
  4. **Clinical practice:** Default choice, Who may benefit, Counselling, Cost per case.
  5. **Every estimate with a 95% CI**, as a plain list with no bars or scale. One line each: outcome, measure, estimate and interval, direction, study and reference number, design. For example: "Bile duct injury · pooled RR 0.97 (0.77–1.21), robotic vs laparoscopic · Acute cholecystitis MA 2026 [1] · Observational". Ratios first, then differences.
  6. **Outcome by outcome:** each outcome's summary, certainty and numbers, all open on the page (nothing to tap).
  7. **Counselling: out of 100 patients**, as plain lines with no dot grid and no Clinician/Patient switch. One line per outcome: "Conversion to open: about 2 of 100 with robotic vs 4 of 100 with laparoscopic (exact 2.1% vs 3.9%). Certainty: Low. [1]", followed by why that certainty and who the numbers apply to.
  8. **Compare the arms**, as a plain list grouped by each study's comparison.
  9. **Details:** Conflicting evidence, Applicability, What would change this answer, The trials (with study details), Other notable findings, Search trail, References.
- **Not carried over (10 Nov):** the chat button, the introduction panel, the progress panel's styling, filters, tabs, pop-up study cards and "Copy as text". **From 11 Nov only the chat button stays out;** the rest come in with the Incisive layout (§2a nos. 1 and 3).
- **References:** numbered, each a link to PubMed.
- **Untraced numbers** carry the words "not traced" beside them. (Not in F4a: by owner decision F4a-5 the completeness check shows counts only and marks nothing on the page.)
- **Completeness check box (F4a, 13 Nov 2026; mock `docs/mocks/f4a-completeness-v3.html`).** The last box of the answer page, after References and the folds, before the closing lines: "Completeness check", then Pending › Running › Done with the current step dark, and at Done one line, for example "16 of 16 numbers found · 4 of 4 trial names match" ("No numbers to check" / "No trial names to check" when there are none). If the check broke, was cut off by a restart, or is not done 60 s after the answer was stored: no dark step and "The check could not be completed. The answer is unchanged." The page keeps its 5 s refresh after the answer shows only while the check is pending or running, 60 s at most. An answer stored before F4a, and a run with no written answer, show no box. Fixed words and counts only; no script.
- **Layout reference:** the Incisive Quick output the owner supplied on 10 Nov (bile leak, robotic vs laparoscopic cholecystectomy in acute cholecystitis).

## 7. What is stored

Tables as built (F0 to F4a; brought in line with the migrations on 11 Nov 2026, 12 Nov 2026 and 13 Nov 2026):

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

| `run_intros` | One row per run's Introduction: status (pending, done, failed), a fixed reason, the text (only when done), times. Pending rows fail `stale` at start-up | `0006_intro_early.sql` (also the `model_calls` step `intro`) |
| `run_checks` | One row per run whose written answer was stored: the completeness check's status (pending, running, done, failed), a fixed reason when failed, the four counts when done, times. No text. Inserted pending in the answer's own transaction; pending and running rows fail `stale` at start-up; the check is not run again | `0007_run_checks.sql` |
| `run_early` | The early summary while the run works: the checked summary and context parts as an answer body. Deleted when the run ends and at start-up | `0006_intro_early.sql` |
| `answers` | The finished answer as JSON, in the shape of Incisive's `AnswerV3` (Quick fields only): the written answer or the plain reason none was written, the references, the search trail, the citation-check counts, the time taken. Written in one transaction with the run's final status and the line "Answer ready". The number-check results join it in F4 | `0005_answers.sql` (also the step kinds `check` and `present`, and the writer's `model_calls` steps) |

- Changed from the section shown in chat: there is no `extractions` table, because Incisive Quick has no separate extraction step. The number-check results live inside the answer, as in Incisive.
- Table changes are numbered SQL files in `migrations/`, applied in order at start-up.

## 8. When things go wrong

| Problem | What the app does | What the user sees |
|---|---|---|
| Gemini returns 429 (per-minute limit) | Waits and retries, up to the run's time limit | "Waiting for the free limit (about N s)" |
| Day's allowance used up | Does not start the run | "Daily free limit reached. Resets at <time> IST." |
| Gemini reply is not valid JSON | One retry of that call | Nothing, unless it fails twice |
| A writer part fails twice | Writes the answer in one piece | "Writer: one part failed; writing in one piece" |
| PubMed or Europe PMC is down | The researcher carries on with the other | "Europe PMC could not be reached; searched PubMed only" |
| No papers found | Ends the run "done" | "No relevant evidence found." |
| App restarts mid-run | Marks the run "stopped" | "This run was stopped. Run again?" |
| Out of time before the writer | Ends "partial" | The references, with a line saying the answer could not be written in time |
| Database down, or start-up work (migrations, owner row) failed | `/healthz` answers 503 `database_unreachable` or `not_ready` (200 `ok` when ready); retries start-up at most every 5 s | "The database could not be reached..." or "The app could not finish starting..." |
| A deploy's new version is not healthy, or the deploy is interrupted | `infra/deploy.sh` restores the previous `.env` and `compose.yaml` and starts the previous version | The previous version; the GitHub deploy shows as failed |

## 9. Stages

Each stage is a small batch on its own branch and pull request, signed off by the owner on a board.

| Stage | What | Done when |
|---|---|---|
| **F0** | Repo skeleton, Postgres, Google sign-in gate, CI, deploy to the VPS | A page loads behind sign-in at the web address |
| **F1** | The four search tools, no model: type a question, see PubMed and Europe PMC results as plain text | Useful on its own at ₹0 |
| **F2** | Gemini layer with limits, the researcher loop and the study list | Picks look sensible on the four benchmark questions |
| **Look** | Greys and the two fonts on the existing pages; menu Ask, Library, Activity. No behaviour change (§2a nos. 2 and 4) | The owner checks the pages on his phone |
| **F3a** | Writer in four parts with the one-piece fallback, the citation check (moved here), the stored answer, the cap of 26 and 21 runs, and the answer on the run page in plain sections. **Built** on `f3a-writer`, not merged | A full written answer is on the page |
| **F3b** | The answer page to the Incisive layout from a mockup, the search trail under Details (§2a no. 1). Live. **F3c:** the Introduction and the early summary (§2a no. 3, D8), built on `f3c-introduction-summary` | The page matches the mockup the owner signed off |
| **Doctors** | The owner's add/remove page, 10 searches a day each, Library and Activity pages (§2a no. 5). Needs the gate decision first | A doctor the owner added can sign in, ask, and see only their own questions |
| **F4a** | The completeness check, code only: counts of numbers printed in the cited abstract and of trial names agreeing with PubMed's record, in one box at the foot of the answer page. No model call; the answer is never changed. **Built** on `f4a-completeness-check`, not merged | The owner compares a count with the abstracts by eye and finds it fair |
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

Full mode, full text of papers, Google search, guideline search on society websites, citation counts (OpenAlex, Semantic Scholar), retraction checks (Crossref), ongoing trials (ClinicalTrials.gov), the doctor's own key, the plugin form. Each can be proposed after F5. (The S1e layout and dashboards were on this list until 11 Nov 2026; they are now in, §2a no. 1.)

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

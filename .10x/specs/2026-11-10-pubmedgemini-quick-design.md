# pubmedgemini: Quick run on free APIs (design)

10 Nov 2026. Status: **design agreed section by section in chat; this written spec awaits the owner's review.** Nothing is built yet.

Source: `plans/2026-11-10-incisive-free-build-handover.md` (Project "Pubmed Incisive"), changed by the owner's decisions of 10 Nov listed in §2.

## 1. Goal

A second, simpler Incisive that costs ₹0 to run.

- It answers a clinical question the way Incisive's Quick run does: a researcher searches PubMed and Europe PMC, a writer writes a structured answer from the abstracts, and code checks the citations and numbers.
- Every model call goes to Google Gemini's free tier, on the Flash-Lite model.
- The page is plain writing. No dashboards, charts or cards.
- Incisive (Rust, `manjunathhari/incisive`) is not touched. It is the yardstick for quality.

**Success:** on the four benchmark questions (hernia, achalasia, cholecystitis, appendicitis) the owner can read a full answer at ₹0 and judge the gap against Incisive Quick side by side.

## 2. Decisions made by the owner (10 Nov 2026)

| # | Decision |
|---|---|
| 1 | Model: Gemini free tier only. **Flash-Lite does every step**, including the write-up. No Flash, no fallback between models. |
| 2 | Scope: **Quick only.** No Full run. |
| 3 | Pipeline: **the same as Incisive's Quick run** (researcher → study list → writer in four parts → citation check → number check → number completion). |
| 4 | Page: **plain writing in the browser.** No S1e layout, no mockup stage. |
| 5 | Language: Python. |
| 6 | Hosting: the app and the database both run on the owner's **Hostinger VPS** (2 CPU, 8 GB, Ubuntu 24.04 with Docker and Traefik). No Google Cloud. |
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
- A run makes 9 to 14 calls (§5), so the allowance is about 35 to 55 runs a day.
- **Privacy:** Google may use free-tier prompts and replies, and human reviewers may read them. The page carries a standing line saying so, and asks the user not to type anything that identifies a patient.

## 4. Shape of the system

**Code.** One repo. Python 3.12, FastAPI, Jinja2 templates, a little JavaScript so the page refreshes while a run works. `httpx` for PubMed and Europe PMC. The `google-genai` SDK for Gemini. Pydantic models for the answer. `psycopg` 3 with plain SQL. `pytest` for tests.

**On the VPS.** One Docker Compose project with three services:

| Service | What it does | Reachable from |
|---|---|---|
| `gate` | `oauth2-proxy`: Google sign-in, lets in only the email addresses on the allow list | The internet, through Traefik (HTTPS) |
| `app` | The FastAPI app | `gate` only, on the internal Docker network |
| `db` | Postgres 17 | `app` only. No published port |

- Traefik, already on the VPS, terminates HTTPS and routes the web address to `gate`.
- The app reads the signed-in email from the header `gate` sets. It is safe to trust because nothing else can reach `app`.
- A user who passes Google sign-in but is not in the `users` table sees "You haven't been set up yet. Ask the owner to add you."

**Runs.** A run is a background task inside the app process. Each step writes its line and result to Postgres as it finishes; the page polls and reads from there. On start-up the app marks any run still "running" as "stopped", and the page shows a "Run again" button.

**Limits.** Two limiters shared by the whole app:
- PubMed: 3 requests a second without a key, 10 with a free NCBI key. Every request carries the registered `tool` and `email` values.
- Gemini: per model, per minute and per day. The day count is kept in Postgres (`model_calls`), so the app refuses to start a run it cannot finish instead of failing halfway. The page shows "Free runs left today: about N".

**Secrets.** The Gemini key, the NCBI key, the Google sign-in client secret, the gate's cookie secret and the database password live in one `.env` file on the VPS, owned by root with mode 600. They are never in the repo, in images or in logs. This stands in for Incisive's Secret Manager.

**Logs.** Question text, search queries, abstracts and model replies are never written to logs. Logs hold counts and fixed kinds only, as in Incisive. The question and the answer are stored only in Postgres, so the owner can reopen past runs.

**Deploy.** Work happens on branches and pull requests. Nothing is merged without the owner's explicit "merge it". On merge to `main`, GitHub Actions runs the tests, builds the image, pushes it to GitHub's container registry, then connects to the VPS with a deploy-only SSH key and runs `docker compose pull && docker compose up -d`. Rollback is the same command with the previous image tag.

**Backups.** A nightly `pg_dump` on the VPS, keeping the last 7 days.

## 5. One Quick run

The same steps as Incisive Quick (`agent/team.rs`, `agent/researcher.rs`, `agent/writer_v3.rs`, `agent/numbers_complete.rs`).

| Step | Done by | Calls |
|---|---|---|
| 1. **Researcher.** A tool-using loop of up to 8 rounds with four tools: `search_pubmed`, `search_europepmc`, `get_abstracts`, `find_related`. Abstracts only | Flash-Lite | 3–8 |
| 2. **Study list.** One structured call over the transcript: up to 10 `selected` (PMID, design, why), plus `considered_not_selected` and `possible_contradictions` | Flash-Lite | 1 |
| 3. **Writer.** Four parts at once over the same input: summary (at a glance, bottom line, clinical practice), context (applicability, what would change), trials (trials, conflicting evidence, notable findings), outcomes | Flash-Lite | 4 |
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
- **Early summary.** The summary part is shown as soon as it has been read and checked, before the other parts finish.
- **One-piece fallback.** If a writer part fails twice, the answer is written in one call instead.
- **Time.** A run stops after 6 minutes. The writer is not started with less than 150 seconds left; the run then ends "partial" with the references.

**Prompts.** The researcher, study-list, writer and number-completion prompts are copied from Incisive. The only edits are the ones Gemini's tool-calling format requires. Any other prompt change is an engine change and is discussed with the owner first.

**Top of every answer:** "Searched PubMed and Europe PMC (free sources); answer written by <model name>".

## 6. The page

Plain HTML: headings, paragraphs, simple lists and one table style for outcomes. Readable on a phone.

- **Ask page:** a question box, the "Free runs left today" line, the privacy line, and a list of past runs.
- **While a run works:** the step lines as plain text, for example "Researcher: 5 searches so far (turn 2 of 8)" and "Writer: writing the answer from 10 studies".
- **Answer page.** The owner's ruling (10 Nov, on seeing an Incisive Quick output page): strip the UI, keep the words, and keep the confidence intervals and the counselling without graphics. In order:
  1. **Results line:** total time, searches run, studies selected (RCTs, meta-analyses), citations checked, numbers traced.
  2. **At a glance.**
  3. **Bottom line**, with its certainty and the reasons for it.
  4. **Clinical practice:** Default choice, Who may benefit, Counselling, Cost per case.
  5. **Every estimate with a 95% CI**, as a plain list with no bars or scale. One line each: outcome, measure, estimate and interval, direction, study and reference number, design. For example: "Bile duct injury · pooled RR 0.97 (0.77–1.21), robotic vs laparoscopic · Acute cholecystitis MA 2026 [1] · Observational". Ratios first, then differences.
  6. **Outcome by outcome:** each outcome's summary, certainty and numbers, all open on the page (nothing to tap).
  7. **Counselling: out of 100 patients**, as plain lines with no dot grid and no Clinician/Patient switch. One line per outcome: "Conversion to open: about 2 of 100 with robotic vs 4 of 100 with laparoscopic (exact 2.1% vs 3.9%). Certainty: Low. [1]", followed by why that certainty and who the numbers apply to.
  8. **Compare the arms**, as a plain list grouped by each study's comparison.
  9. **Details:** Conflicting evidence, Applicability, What would change this answer, The trials (with study details), Other notable findings, Search trail, References.
- **Not carried over:** the chat button, the introduction panel, the progress panel's styling, filters, tabs, pop-up study cards and "Copy as text".
- **References:** numbered, each a link to PubMed.
- **Untraced numbers** carry the words "not traced" beside them.
- **Layout reference:** the Incisive Quick output the owner supplied on 10 Nov (bile leak, robotic vs laparoscopic cholecystectomy in acute cholecystitis).

## 7. What is stored

| Table | Holds |
|---|---|
| `users` | Email addresses set up to use the app, with a role |
| `runs` | One row per question: question text, status, error kind, timings, model |
| `run_steps` | The step lines shown on the page |
| `papers` | Each paper once: PMID, DOI, title, abstract, design, year, journal. A repeat search does not fetch it again |
| `run_papers` | Which papers a run selected and which it only considered, with the reason |
| `answers` | The finished answer as JSON, in the shape of Incisive's `AnswerV3` (Quick fields only), including the search trail and number-check results |
| `model_calls` | One row per Gemini call: model, day, step, token counts. No text |

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

## 9. Stages

Each stage is a small batch on its own branch and pull request, signed off by the owner on a board.

| Stage | What | Done when |
|---|---|---|
| **F0** | Repo skeleton, Postgres, Google sign-in gate, CI, deploy to the VPS | A page loads behind sign-in at the web address |
| **F1** | The four search tools, no model: type a question, see PubMed and Europe PMC results as plain text | Useful on its own at ₹0 |
| **F2** | Gemini layer with limits, the researcher loop and the study list | Picks look sensible on the four benchmark questions |
| **F3** | Writer in four parts and the plain answer page | A full answer is on the page |
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

Full mode, full text of papers, Google search, guideline search on society websites, citation counts (OpenAlex, Semantic Scholar), retraction checks (Crossref), ongoing trials (ClinicalTrials.gov), the doctor's own key, the plugin form, the S1e layout and dashboards. Each can be proposed after F5.

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

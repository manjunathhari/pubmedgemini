# Change log

Live: F0, F1, F2, the look, Library and Activity (merged and deployed on 10 and 11 Nov 2026), F3a (the writer) and F3b (the answer page). F3c (the Introduction and the early summary) is on its branch `f3c-introduction-summary` and is not merged. Entries start with the first merge to `main` that changes the running app.

## F3c: the Introduction panel and the early summary (branch `f3c-introduction-summary`, not merged yet)

- While a question runs, a short Introduction appears under the question within about half a minute: what the question asks, why it matters and a few lines on the disease, written by the model from the question alone (Incisive's instructions, word for word). It says so: "Background only, written by the model from your question alone. It is not drawn from the studies and carries no citations." On the finished page it sits folded above the steps. If it cannot be written (Gemini busy, a refusal, a timeout, anything), nothing is shown and the question carries on as before.
- It is one of the question's 26 Gemini attempts (one try, 30 s, never retried). "Free runs left today" still says about 21 on a fresh day; a typical question now sends 14 requests instead of 13.
- When the summary part of the answer has been read and its citations checked, the working page shows it marked "Early": At a glance (without its key numbers: "The key numbers are shown with the full answer."), Bottom line, and, once that part is in, Applicability and What would change this answer, with a line saying what is still being written. The full answer replaces it at the next refresh. If a part fails, the early summary is withdrawn at once. It is never kept after the question ends, and an app restart clears it.
- Answers stored before this change look exactly as they did. Nothing to do on the VPS: the new tables are added at start-up.
- Review fixes (12 Nov 2026): citation numbers in the early summary are plain text (there is no reference list to jump to yet); the finished page still links them. Text the model writes (the early summary and the answer) is stored without hidden characters, so a stray one can no longer stop an answer being saved.

## F1: search tools and the plain results page (branch `f1-search-tools`, not merged yet)

- After sign-in, one search box. The text is sent as the search to PubMed and Europe PMC (free); the page lists up to 20 papers, merged by PMID then DOI, each with title, journal, year, design label, publication types, a PubMed (or DOI) link, "Retracted" when so, and the abstract.
- If one source cannot be reached, the page says so and shows the other. If both cannot be reached, a plain 503 message.
- Text outside the query rule is refused with the rule's message, before anything is sent. Owner decision (10 Nov 2026): on this page only, trailing question marks are dropped before the check.
- Abstracts are kept in a `papers` table (served for 30 days, removed after 90 at start-up).
- New optional settings `NCBI_API_KEY` and `NCBI_EMAIL`; the app runs without them. Nothing to do on the VPS (runbook: "Optional: the NCBI key").
- Review fixes (11 Nov 2026): a slow source now times out after 20 s in total per attempt (in the worst case about 40 s per attempt, when a single read stalls just before the limit); when PubMed is down the page no longer waits for it a second time and only says "titles only" when an abstract is really missing; an odd reply from a source or an unexpected error shows a plain message instead of failing; a search posted from another site is refused; the sign-in cookie is SameSite=Lax.
- Re-check fixes (11 Nov 2026): a search posted from a sibling subdomain is refused too; if the sign-in expired while typing, the browser comes back to the search box instead of a raw error (the typed text is lost).

## F2: questions, the researcher and the study list (branch `f2-researcher`, not merged yet)

- After sign-in, the home page has "Ask a question" above the F1 search (now headed "Key-word search", unchanged), the line "Free runs left today: about N", the privacy line, and "Your questions" (past runs, newest first).
- A question goes to Gemini Flash-Lite (free tier), which runs Incisive Quick's researcher: up to 8 rounds of PubMed and Europe PMC searches with the four F1 tools, then Incisive's study list. Code checks the list (only papers the run really saw, no duplicates, at most 10). No answer is written yet (F3).
- The run page shows the step lines while it works and refreshes itself every 5 seconds (no JavaScript). When it ends: the selected studies in the model's order (title, journal, year, the model's design label, its one-line reason, publication types, PubMed link, abstract), "Considered, not selected", "Possible contradictions", the search trail and the model's name. Each failure has a plain message.
- One question runs at a time for the whole app. A restart marks a running question "stopped". A stopped, failed or partial question has a "Run again" button (owner decision, 11 Nov 2026).
- Before a question leaves the app it is checked for details that could identify a patient (Incisive's check, copied rule for rule). If anything is found, nothing is stored or sent, and the page says what kind of detail was found, never the text itself.
- The free day's Gemini calls are counted in a `model_calls` table (no text); a question is refused before it starts when the day's allowance is used up ("Daily free limit reached. Resets at HH:MM IST.").
- One question sends at most 14 requests to Gemini (until F3a), retries included (owner decision, 11 Nov 2026). If Gemini stays busy and a question reaches that, it stops and says "Gemini stayed busy, so this run used up its share of today's free calls. Please run the question again." So one question can never use more than the day has left. "Free runs left today" shows the same number as before (45 on a fresh day, until F3a).
- Review fix (11 Nov 2026): when Gemini sends back something that is not readable, no part of its reply is kept with the error.
- Never logged: the question, queries, titles, abstracts, the model's text, tool results, Google's error messages, the Gemini key. Tested in the app and under the real server.
- New optional settings `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_RPM`, `GEMINI_TPM`, `GEMINI_RPD`. The app runs without them; without the key the question box says "Questions are not set up yet" and the key-word search works. One step on the VPS to add the key (runbook: "The Gemini key (for questions)").

## F2 search checklist (branch `f2-search-checklist`, not merged yet)

- The researcher's instructions get one added block, approved by the owner on 11 Nov 2026 (not from Incisive): before it stops, the researcher checks that it has run a broad trial search, the same search for the last five years, a five-year search for reviews and meta-analyses, at least one Europe PMC search, a search for each key trial's longer follow-up, and similar articles for the two most important trials; it re-runs a search that finds fewer than 40 papers with fewer terms, and selects a guideline only when the question asks about practice recommendations. The five years count back from the current year (2021 to 2026 this year).
- Nothing else changes: the same 8 rounds, the same 14-request cap (until F3a), the same tools and the same study-list step. Nothing to do on the VPS.

## Look: greys and two fonts (branch `look-greys-fonts`, not merged yet)

- Every page gets the new look the owner signed off on 11 Nov 2026: greys only, Hanken Grotesk for the interface and Source Serif 4 for text, both served by the app itself (no outside font service). Sections sit in white cards on a light grey ground; the page works at phone width.
- A header on every page: a small "P" tile, "pubmedgemini" and "Educational tool only". This is the only new wording; no other words, links or behaviour changed.
- The page's own styles moved from the page into a stylesheet at `/static/app.css`; the fonts are under `/static/fonts/`. They sit behind the sign-in gate like every other path.
- Three older page tests were updated because this look replaces the owner's 10 Nov "plain pages" ruling: they now accept the stylesheet link, class names and wrapper elements, and check the width in the stylesheet. Their checks on upstream text (nothing from PubMed or Europe PMC is rendered as markup) are unchanged, and now also check that every class value is plain letters, digits, hyphens and spaces.
- Each page view now also makes a few requests for the stylesheet and fonts, which add `request method=GET route=... status=200` lines to the log. Fixed words only, no paths or text; left as they are.
- Nothing to do on the VPS.

## Library and Activity pages, and the menu (branch `library-activity`, not merged yet)

- Every page a signed-in, set-up user sees now has a menu under the header: Ask, Library, Activity. The current one is bold with an underline. A plain message page for a signed-in user (such as "That question wasn't found") shows the menu with no item marked; pages for someone not set up or not signed in show the header without the menu. The app's name on the page stays "pubmedgemini".
- New Library page (`/library`): your past questions, newest first, up to 50, each as before (the question's first words as a link to its page, the date in India time, and the status). "No questions yet." when there are none. A run page and the key-word results page highlight Library and Ask in the menu.
- New Activity page (`/activity`): a card "My questions, last 14 days" with one row per day (India time), today first, zeros included, and the number of questions you asked that day. A question counts on the India day it was asked, whatever happened to it.
- The "Your questions" list is gone from the home page; it lives on Library. Nothing else on the home page changed.
- Each page shows only the signed-in user's own questions and counts. Nothing new is logged. No JavaScript.
- Nothing to do on the VPS.

## F3b: the answer page on the live writer (branch `f3b-answer-page`, not merged yet)

- A finished question's page is now laid out as the mock the owner signed off (v6): the answer first, in cards, with the steps folded at the top and the selected studies, the studies considered and the possible contradictions folded at the bottom. Nothing is lost from the old page: it is the same content, moved and folded.
- "Why this conclusion": every ratio or difference that has a 95% interval is drawn as a short line on a scale (dashed mark at 1 or 0), beside the outcomes, each of which opens with a tap. "Counselling dashboard": out of 100 patients like those in the trials, solid squares for one arm and hatched squares for the other, beside "Compare the arms".
- A number is drawn only when the app is sure what it is. Anything else is shown as text under its outcome (or in Compare the arms) with the reason, for example "Not drawn: no interval." Owner decisions of 12 Nov: a hazard ratio, odds ratio, risk ratio, relative risk or rate ratio named at the start of its label is drawn even when the writer left its unit blank, but only when the label is plainly that one ratio: "Hazard ratio for overall survival" is drawn; "Relative risk reduction ...", "Hazard ratio not reported ...", "Odds ratio difference" or "Hazard ratio change from baseline" are shown as text (R1, narrowed after the review); squares are drawn for "Group A" / "Group B" when the trial's comparison names "(group A)" and "(group B)" (S1), and for "Experimental" / "Standard" / "Standard of care" / "Control" arms when the trial's comparison itself uses those words on the matching sides (S2).
- In "Out of 100" the first treatment of the trial's comparison always has the solid squares and the second the hatched ones, whatever order the writer listed them in. When the trial's comparison or an arm is worded with a negation ("no", "not", "non-", "without", "none", "neither", "nor"), the pair is not drawn as squares and stays a named line in "Compare the arms" with that reason, because a word after "no" or "non" names the opposite treatment. A number given with only one end of its interval reads, as on the old page, "(CI from x)" or "(CI to x)".
- Greys only: certainty labels print their word (High dark, Moderate light, Low outlined, Very low dashed outline). On a computer the page's content is wider and the cards sit two by two; on a phone they stack, with no sideways scrolling.
- Answers already stored show in the new layout without running them again. Runs without an answer look as before. No JavaScript.
- Nothing changes in how the answer is written: the same calls, the same "Free runs left today", the same time limits, no new table. Never logged: anything from the answer; the page logs only "answer_page kind=row_not_derived rows=N" when a line could not be placed.
- Nothing to do on the VPS.

## F3a: the writer (branch `f3a-writer`, not merged yet)

- A finished question now shows a written answer on its page, in plain sections: At a glance, Bottom line, Clinical practice, the outcomes, the trials and the references. The page still lists the studies, as before. The Incisive layout, the Introduction and the number check come later (F3b, F4).
- Gemini Flash-Lite writes the answer in four parts at once, from Incisive's prompts, word for word. If a part fails in a way that is not final and at least 150 seconds are left, the answer is written in one piece instead. The step lines show "Writer: writing the answer from N studies", a "Citation check:" line and "Answer ready".
- The citation check now removes a citation, a number or a trial row that points to a study that was not selected. Owner decision (11 Nov 2026), as Incisive does. This is the one stated exception to "flag, never delete".
- A question may now send at most 26 requests to Gemini, retries included (was 14). "Free runs left today" shows 21 on a fresh day (was 45). A normal question makes 13 requests: 8 for the search, 1 for the study list, 4 for the writer. The 21 counts the worst case, so a question admitted late in the day can never pass the day's limit.
- If the daily limit, the key or billing is refused, or the question's 26 requests are used up, while the answer is written, the run ends at once with the same plain message as before. The one-piece retry is not tried then (owner decision Q2, 11 Nov 2026).
- If no answer could be written, the page shows a short plain message, and the run is marked partial: not enough time left (under 150 s), the answer was longer than the writer's limit, or the answer could not be read. If the records of the selected studies cannot be fetched from PubMed, the run fails and says "PubMed could not be reached to fetch the selected studies, so no answer could be written. Please run the question again." A failed run keeps its study list.
- Answers are stored in a new `answers` table (migration 0005, applied at start-up). The answer and the run's final status are stored together, so the page never shows one without the other.
- Never logged: the question, abstracts, and anything the writer wrote or Gemini said. Tested in the app and under the real server, with a marker in each part's text, the one-piece text, a blocked reply and an answer the database refuses.
- Nothing to do on the VPS. No new setting: `compose.yaml` and `.env.example` are unchanged.

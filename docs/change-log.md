# Change log

Nothing is deployed yet. Entries start with the first merge to `main` that changes the running app.

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
- One question runs at a time for the whole app. A restart marks a running question "stopped", with a "Run again" button.
- Before a question leaves the app it is checked for details that could identify a patient (Incisive's check, copied rule for rule). If anything is found, nothing is stored or sent, and the page says what kind of detail was found, never the text itself.
- The free day's Gemini calls are counted in a `model_calls` table (no text); a question is refused before it starts when the day's allowance is used up ("Daily free limit reached. Resets at HH:MM IST.").
- Never logged: the question, queries, titles, abstracts, the model's text, tool results, Google's error messages, the Gemini key. Tested in the app and under the real server.
- New optional settings `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_RPM`, `GEMINI_TPM`, `GEMINI_RPD`. The app runs without them; without the key the question box says "Questions are not set up yet" and the key-word search works. One step on the VPS to add the key (runbook: "The Gemini key (for questions)").

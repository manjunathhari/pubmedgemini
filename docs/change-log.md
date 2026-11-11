# Change log

Nothing is deployed yet. Entries start with the first merge to `main` that changes the running app.

## F1: search tools and the plain results page (branch `f1-search-tools`, not merged yet)

- After sign-in, one search box. The text is sent as the search to PubMed and Europe PMC (free); the page lists up to 20 papers, merged by PMID then DOI, each with title, journal, year, design label, publication types, a PubMed (or DOI) link, "Retracted" when so, and the abstract.
- If one source cannot be reached, the page says so and shows the other. If both cannot be reached, a plain 503 message.
- Text outside the query rule is refused with the rule's message, before anything is sent. Owner decision (10 Nov 2026): on this page only, trailing question marks are dropped before the check.
- Abstracts are kept in a `papers` table (served for 30 days, removed after 90 at start-up).
- New optional settings `NCBI_API_KEY` and `NCBI_EMAIL`; the app runs without them. Nothing to do on the VPS (runbook: "Optional: the NCBI key").
- Review fixes (11 Nov 2026): a slow source now times out after 20 s in total per attempt (in the worst case about 40 s per attempt, when a single read stalls just before the limit); when PubMed is down the page no longer waits for it a second time and only says "titles only" when an abstract is really missing; an odd reply from a source or an unexpected error shows a plain message instead of failing; a search posted from another site is refused; the sign-in cookie is SameSite=Lax.

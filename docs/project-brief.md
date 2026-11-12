# Project brief: pubmedgemini

- **Goal:** Incisive's Quick run at ₹0 a run. Free PubMed and Europe PMC for search; Gemini Flash-Lite (free tier) for every model step; the answer page of the owner's frozen mock (v4, 12 Nov 2026, `docs/mocks/f3-answer-page-v4.html`), drawn by code, no JavaScript.
- **Full design:** `.10x/specs/2026-11-10-pubmedgemini-quick-design.md`. Decision record: `.10x/adrs/001-single-vps-python-gemini-flash-lite.md`.
- **Stack:** Python 3.12, FastAPI, Postgres 17, oauth2-proxy, Docker Compose on the owner's VPS.
- **Clinical field:** surgical evidence questions. No patient data is stored or sent.
- **Known risks:** the researcher loop on a small model; free limits can change.
- **Pass marks:** per stage, spec §9. Final: the owner judges answers side by side with Incisive Quick on four benchmark questions.
- **Must not be touched:** `manjunathhari/incisive` (read-only reference). Shared services on the VPS.
- **Working rules:** spec §14. Merge only on the owner's words "merge it". Every engine change is discussed first.
- **Keep out of this repo:** secrets, server names and addresses, account ids, question text, model replies, run outputs.

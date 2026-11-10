# ADR-001: One VPS, Python, Gemini Flash-Lite free tier

**Status:** Accepted
**Date:** 2026-11-10
**Feature:** pubmedgemini-quick
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
The owner wants Incisive's Quick run at ₹0 a run, coded quickly, without touching Incisive. He ruled on 10 Nov 2026: Gemini free tier only, Flash-Lite for every step, Quick only, Python, database on his Hostinger VPS, app on the same VPS, plain-text page.

## Decision
A Python 3.12 FastAPI app, Postgres 17 and an `oauth2-proxy` Google sign-in gate run as one Docker Compose project on the owner's Hostinger VPS behind the existing Traefik. Every model call goes to Gemini Flash-Lite on the free tier. The pipeline mirrors Incisive Quick: researcher loop, study list, four-part writer, citation check, number check, number completion.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| App on Cloud Run, DB on the VPS | Same sign-in (IAP) as Incisive | DB port open to the internet; Cloud Run has no fixed address to allow-list without a paid add-on | Security weakness for little gain |
| TypeScript on Vercel | Matches the registry app | 1–2 minute runs need background jobs | More moving parts |
| Flash for the write-up | Better writing | 20 requests a day: about 5 runs | Owner chose Flash-Lite only |
| Fixed search plan in code instead of a researcher loop | Fewer calls, easier for a small model | Not the same as Incisive Quick | Owner asked for the same run; kept as the fallback if F2 fails |

## Consequences
### Positive
- ₹0 a run; about 35–55 runs a day.
- Database is never exposed to the internet.
- No Google Cloud to set up.

### Negative
- Quality depends on a small model; expected below Incisive.
- One 2-CPU VPS shared with Gemma; sized for one user.
- Secrets live in a root-only `.env` file, not a managed secret store.

### Risks
- Flash-Lite may not sustain a tool-using search loop. Tested first at F2; fallback above needs a new owner decision.
- Google can change free limits. Limits are settings; the page shows what is left.

## Dependencies
- Depends on the VPS's Traefik, a web address pointed at the VPS, and a Google sign-in client.
- Constrains: no feature may need a paid API without a new decision.

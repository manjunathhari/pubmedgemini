# Architect: pubmedgemini-quick

- **Components:** `gate` (oauth2-proxy) → `app` (FastAPI) → `db` (Postgres 17), one Compose project behind Traefik. Only `gate` is reachable from outside.
- **Runs:** background task in the app process; state in Postgres; page polls. Runs left "running" at start-up are marked "stopped".
- **Limits:** shared PubMed limiter; Gemini limiter per minute and per day, day count in `model_calls`.
- **Failure modes:** spec §8.
- **Data:** spec §7.

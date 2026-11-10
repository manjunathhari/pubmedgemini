# Senior engineer: pubmedgemini-quick

- **F0 approach:** see the F0 plan. App factory `create_app(settings)`; identity from the gate's `X-Forwarded-Email` header; dev header only when `APP_ENV=dev`.
- **Tricky parts in F0:** Traefik names differ per VPS, so labels are read from `.env`; the app must never be reachable except through the gate.
- **Later stages:** port Incisive's validation and checks with their test cases before porting prompts.

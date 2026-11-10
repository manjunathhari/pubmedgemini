# Staff engineer: pubmedgemini-quick

- **Layout:** `src/pubmedgemini/`, one responsibility per module; tests in `tests/` mirror modules.
- **Config:** one `Settings` class read from the environment. No secret has a default.
- **SQL:** plain SQL through `psycopg` 3; numbered files in `migrations/`, applied in order at start-up.
- **Logging:** counts and fixed kinds only. Never question text, queries, abstracts or model replies. A test enforces it.
- **Model untrusted:** every model reply is validated in code before use.
- **Lint and tests:** `ruff` and `pytest` must pass in CI before any merge.

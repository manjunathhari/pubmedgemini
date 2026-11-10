# pubmedgemini

A Quick evidence run on free APIs: PubMed and Europe PMC for search, Gemini Flash-Lite (free tier) for reading and writing. Design: `.10x/specs/2026-11-10-pubmedgemini-quick-design.md`.

## Run the tests locally

You need Python 3.12 or newer and a Postgres server for the database tests.

```
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
ruff check .
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest -q -rs
```

The database tests drop and recreate the `public` schema of the database in `TEST_DATABASE_URL`. Use a throwaway database. If the variable is not set, those tests are skipped; CI treats a skip as a failure.

## Deploy and operate

One-time VPS setup, rollback and restore: [`infra/README.md`](infra/README.md).

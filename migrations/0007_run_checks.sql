-- F4a: the completeness check, code only. One row per run whose written answer was stored;
-- inserted `pending` in the answer's own transaction, moved by the run's thread to `running`
-- and then `done` with four counts, or `failed` with a fixed kind. 0001-0006 are not edited.
-- The table holds counts and fixed words only: no question, no model text, no number or name
-- from an answer. `answers` is never changed by the check.
CREATE TABLE run_checks (
    run_id        UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    reason        TEXT CHECK (reason ~ '^[a-z_]{1,32}$'),   -- a fixed kind, only when failed
    numbers_found INT,
    numbers_total INT,
    names_match   INT,
    names_total   INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    CHECK ((status = 'done') = (numbers_found IS NOT NULL AND numbers_total IS NOT NULL
                                AND names_match IS NOT NULL AND names_total IS NOT NULL)),
    CHECK (numbers_found BETWEEN 0 AND numbers_total AND names_match BETWEEN 0 AND names_total),
    CHECK ((status = 'failed') = (reason IS NOT NULL))
);

-- One row per Gemini generateContent attempt, for the free day's count. No text column: the
-- prompt, the reply and Google's error message are never stored here; only the model id, the
-- step, the attempt number, token counts, latency and a fixed status kind.
-- The row is written as `sent` before the request goes out (so a crash still counts it), then
-- set to `ok` or a GeminiError kind. The free day resets at midnight Pacific (spec §3), so the
-- count is kept by `quota_day`; both days are worked out from `called_at` by Postgres.
CREATE TABLE model_calls (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model          TEXT NOT NULL CHECK (model ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
    called_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    utc_day        DATE NOT NULL GENERATED ALWAYS AS ((called_at AT TIME ZONE 'UTC')::date) STORED,
    quota_day      DATE NOT NULL
        GENERATED ALWAYS AS ((called_at AT TIME ZONE 'America/Los_Angeles')::date) STORED,
    step           TEXT NOT NULL
        CHECK (step IN ('researcher', 'study_list', 'study_list_nudge', 'study_list_retry')),
    attempt        SMALLINT NOT NULL CHECK (attempt BETWEEN 1 AND 3),
    prompt_tokens  INT,
    output_tokens  INT,
    thought_tokens INT,
    latency_ms     INT,
    status_kind    TEXT NOT NULL CHECK (status_kind ~ '^[a-z_]{1,32}$')
);
CREATE INDEX model_calls_day ON model_calls (model, quota_day);

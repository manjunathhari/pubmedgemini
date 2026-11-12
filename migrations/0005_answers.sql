-- The written answer (F3). `answers.body` is the only table holding model text besides the
-- clipped notes in `run_papers.note`: the answer lives only in Postgres so the owner can reopen
-- past runs, and never in a log line (spec §4 "Logs"). The body is one JSON document in the
-- shape of Incisive's `AnswerV3` (Quick fields only); the question stays in `runs.question`.
-- `written` says how the answer was written (four parts or one piece); null when nothing was.
CREATE TABLE answers (
    run_id     UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    version    SMALLINT NOT NULL DEFAULT 3 CHECK (version = 3),
    body       JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    written    TEXT CHECK (written IN ('parts', 'one_piece')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Step kinds: Incisive `runs/mod.rs` `StepKind`, the five Quick uses (`check` is F4's).
ALTER TABLE run_steps
    DROP CONSTRAINT run_steps_kind_check,
    ADD CONSTRAINT run_steps_kind_check
        CHECK (kind IN ('search', 'note', 'present', 'write', 'check'));

-- The writer's call steps: four parts, their retries, the one-piece fallback and its retry.
ALTER TABLE model_calls
    DROP CONSTRAINT model_calls_step_check,
    ADD CONSTRAINT model_calls_step_check
        CHECK (step IN ('researcher', 'study_list', 'study_list_nudge', 'study_list_retry',
                        'writer_trials', 'writer_outcomes', 'writer_context', 'writer_summary',
                        'writer_trials_retry', 'writer_outcomes_retry', 'writer_context_retry',
                        'writer_summary_retry', 'writer_one_piece', 'writer_one_piece_retry'));

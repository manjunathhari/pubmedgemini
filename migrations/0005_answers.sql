-- The written answer (Incisive migrations/0005_runs.sql `run_answers` and
-- 0010_run_answer_version.sql), the two step kinds the writer adds, and the model_calls steps of
-- the writer's four parts and its one-piece fallback. 0003 and 0004 are not edited: their
-- CHECKs are dropped by their catalog names and added again with the longer lists.
-- `answers.body` holds the answer as JSON (model text, written by the writer and checked by
-- code); it is read by the run page only and never logged (spec section 4: Postgres only).
CREATE TABLE answers (
    run_id     UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    version    SMALLINT NOT NULL CHECK (version = 3),
    body       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Incisive `StepKind`: the five Quick uses.
ALTER TABLE run_steps DROP CONSTRAINT run_steps_kind_check;
ALTER TABLE run_steps ADD CONSTRAINT run_steps_kind_check
    CHECK (kind IN ('search', 'note', 'write', 'check', 'present'));

-- The four researcher steps, then each writer part and its retry, then the one piece and its
-- retry (the part names are the four parts of the writer: trials, outcomes, context, summary).
ALTER TABLE model_calls DROP CONSTRAINT model_calls_step_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_step_check
    CHECK (step IN (
        'researcher', 'study_list', 'study_list_nudge', 'study_list_retry',
        'writer_trials', 'writer_trials_retry',
        'writer_outcomes', 'writer_outcomes_retry',
        'writer_context', 'writer_context_retry',
        'writer_summary', 'writer_summary_retry',
        'writer_one_piece', 'writer_one_piece_retry'));

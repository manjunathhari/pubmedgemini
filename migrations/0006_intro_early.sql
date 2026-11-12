-- F3c: the Introduction (Incisive migrations/0015_run_intros.sql `run_intros`, without the
-- cost and request columns: no money here) and the early summary (Incisive `run_drafts` part
-- `early`). 0001-0005 are not edited: the model_calls step CHECK of 0005 is dropped by its
-- catalog name and added again with `intro`.
-- `run_intros.text` and `run_early.body` hold model text (the Introduction, and the checked
-- summary and context parts); both are read by the run page only and never logged (spec
-- section 4: Postgres only). `reason` is a fixed kind, never model text.
CREATE TABLE run_intros (
    run_id      UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    reason      TEXT CHECK (reason ~ '^[a-z_]{1,32}$'),
    text        TEXT CHECK (char_length(text) <= 16000),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CHECK ((status = 'done') = (text IS NOT NULL))
);

-- One early summary a run, while it runs; deleted when the run ends, whatever the end.
CREATE TABLE run_early (
    run_id     UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    body       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE model_calls DROP CONSTRAINT model_calls_step_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_step_check
    CHECK (step IN (
        'researcher', 'study_list', 'study_list_nudge', 'study_list_retry',
        'writer_trials', 'writer_trials_retry',
        'writer_outcomes', 'writer_outcomes_retry',
        'writer_context', 'writer_context_retry',
        'writer_summary', 'writer_summary_retry',
        'writer_one_piece', 'writer_one_piece_retry',
        'intro'));

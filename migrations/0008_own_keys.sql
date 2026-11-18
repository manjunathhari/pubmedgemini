-- F7: each doctor runs questions on their own Gemini key (owner decisions 1A, 2A, 18 Nov
-- 2026). No key is ever stored: no table here, or anywhere, holds one.
-- 0001-0007 are not edited.

-- A doctor who signs in for the first time gets a row with this role (F7b).
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('owner', 'surgeon', 'fellow', 'user'));

-- The free day is counted per doctor: each key has its own quota. Older rows have no user.
ALTER TABLE model_calls ADD COLUMN user_id BIGINT REFERENCES users(id);
CREATE INDEX model_calls_user_day ON model_calls (user_id, model, quota_day);

-- The one-time check made when a doctor saves a key (/key).
ALTER TABLE model_calls DROP CONSTRAINT model_calls_step_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_step_check
    CHECK (step IN (
        'researcher', 'study_list', 'study_list_nudge', 'study_list_retry',
        'writer_trials', 'writer_trials_retry',
        'writer_outcomes', 'writer_outcomes_retry',
        'writer_context', 'writer_context_retry',
        'writer_summary', 'writer_summary_retry',
        'writer_one_piece', 'writer_one_piece_retry',
        'intro', 'key_check'));

-- One active run per doctor (was: one for the whole app). The app-wide cap, MAX_ACTIVE_RUNS,
-- is checked by runs.create_run under an advisory lock.
DROP INDEX runs_one_active;
CREATE UNIQUE INDEX runs_one_active_per_user ON runs (user_id)
    WHERE status IN ('queued', 'running');

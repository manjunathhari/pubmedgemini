-- Questions and their researcher runs (Incisive migrations/0005_runs.sql `runs` and
-- `run_steps`, plus the search trail, the papers the model saw and the study list, which
-- Incisive keeps elsewhere). No table holds model text except the clipped notes in
-- `run_papers.note` (the model's one-line why, reason or contradiction note, at most 500
-- characters); `runs.question` holds the user's question. Step lines are written by code.
CREATE TABLE runs (
    id          UUID PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    question    TEXT NOT NULL CHECK (char_length(question) BETWEEN 1 AND 4000),
    mode        TEXT NOT NULL DEFAULT 'quick' CHECK (mode = 'quick'),
    status      TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'partial', 'failed', 'stopped')),
    error_kind  TEXT CHECK (error_kind ~ '^[a-z_]{1,40}$'),
    stop_reason TEXT
        CHECK (stop_reason IN ('end_turn', 'turn_cap', 'wrap_up', 'turn_failed', 'truncated',
                               'other')),
    rounds      INT,
    model       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX runs_user_created ON runs (user_id, created_at DESC);
-- One active run for the whole app (Incisive's is per user): one free quota, one pacer.
CREATE UNIQUE INDEX runs_one_active ON runs ((true)) WHERE status IN ('queued', 'running');

-- The step lines shown on the page (Incisive `StepKind`: the three F2 uses).
CREATE TABLE run_steps (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq    INT NOT NULL,
    at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind   TEXT NOT NULL CHECK (kind IN ('search', 'note', 'present')),
    line   TEXT NOT NULL CHECK (char_length(line) <= 160),
    PRIMARY KEY (run_id, seq)
);

-- The search trail, recorded by code as each search tool returns.
CREATE TABLE run_searches (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq    INT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('PubMed', 'Europe PMC', 'PubMed related')),
    query  TEXT NOT NULL CHECK (char_length(query) <= 300),
    hits   BIGINT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

-- Every PMID the model saw in a tool result this run, with public bibliographic fields.
CREATE TABLE run_seen (
    run_id  UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    pmid    TEXT NOT NULL CHECK (pmid ~ '^[0-9]{1,9}$'),
    title   TEXT,
    journal TEXT,
    year    INT,
    PRIMARY KEY (run_id, pmid)
);

-- The checked study list: selected (with the model's design label), considered and
-- possible contradictions, in the model's order (`rank`).
CREATE TABLE run_papers (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    role   TEXT NOT NULL CHECK (role IN ('selected', 'considered', 'contradiction')),
    rank   INT NOT NULL,
    pmid   TEXT NOT NULL CHECK (pmid ~ '^[0-9]{1,9}$'),
    design TEXT CHECK (design IN ('RCT', 'SR_MA', 'other')),
    note   TEXT NOT NULL CHECK (char_length(note) <= 500),
    PRIMARY KEY (run_id, role, pmid),
    CHECK ((role = 'selected') = (design IS NOT NULL))
);

-- A deleted run keeps its calls in the day's count.
ALTER TABLE model_calls ADD COLUMN run_id UUID REFERENCES runs(id) ON DELETE SET NULL;

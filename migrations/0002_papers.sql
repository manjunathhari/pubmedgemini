-- Public bibliographic data only; no user linkage.
-- `design` is advisory: worked out when the row is written, from the publication types the
-- cache was given (PubMed's). The page recomputes the label from the merged types on
-- display; F1 never reads this column.
CREATE TABLE papers (
    pmid       TEXT PRIMARY KEY CHECK (pmid ~ '^[0-9]{1,9}$'),
    pmcid      TEXT,
    doi        TEXT,
    title      TEXT NOT NULL,
    authors    TEXT[] NOT NULL DEFAULT '{}',
    journal    TEXT,
    year       INT,
    pub_types  TEXT[] NOT NULL DEFAULT '{}',
    design     TEXT NOT NULL CHECK (design IN ('guideline', 'systematic review', 'meta-analysis',
                                               'RCT', 'prospective cohort', 'other')),
    abstract   TEXT,
    retracted  BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX papers_fetched_at ON papers (fetched_at);

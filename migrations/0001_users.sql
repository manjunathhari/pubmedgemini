CREATE TABLE users (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE CHECK (email = lower(btrim(email))),
    role       TEXT NOT NULL CHECK (role IN ('owner', 'surgeon', 'fellow')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The web app's own data (see ~/org/20261111_dgi-search-plan.org, Phase 2).
-- Applied when the web app starts. Safe to run again.
--
-- Unlike the dgi_* schemas this cannot be rebuilt from the mirror: it is the
-- record of what people asked and what they were told.

CREATE SCHEMA IF NOT EXISTS app;

-- One row per question asked. Its id is the address of the answer page (/a/<id>).
CREATE TABLE IF NOT EXISTS app.answer (
    id           text PRIMARY KEY,
    question     text NOT NULL,
    qkey         text NOT NULL,            -- the question lower-cased, without accents: for the cache
    status       text NOT NULL,            -- working, done, failed
    answer_md    text,                     -- the model's sentences (Markdown, shown as text only)
    tables       jsonb NOT NULL DEFAULT '[]',   -- [{title, headings, columns, rows, sql}] from the queries
    sources      jsonb NOT NULL DEFAULT '[]',   -- the CSO tables queried
    steps        jsonb NOT NULL DEFAULT '[]',   -- [{say, tool, sql}] for "how this was worked out"
    provider     text,
    model        text,
    own_key      boolean NOT NULL DEFAULT false,  -- answered with the person's own API key (the key is never stored)
    tokens_in    int,
    tokens_out   int,
    cost_usd     numeric(10, 5),           -- what it cost the site; 0 when own_key
    seconds      real,
    error        text,
    client       text,                     -- hash of address + day: for the daily limit, not for tracing anyone
    feedback     smallint,                 -- 1 right, -1 wrong, NULL not given
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS answer_cache ON app.answer (qkey, created_at DESC) WHERE status = 'done';
CREATE INDEX IF NOT EXISTS answer_client ON app.answer (client, created_at);
CREATE INDEX IF NOT EXISTS answer_created ON app.answer (created_at);

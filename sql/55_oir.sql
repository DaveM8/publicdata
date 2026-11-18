-- The Oireachtas record from the Yes Deputy server: every embedded chunk of
-- Dáil, Seanad and committee debates since 1919 and of written and oral
-- answers since 1990, with its OpenAI vector, plus the whole text of recent
-- answers. Filled by `dgi-explore load-oireachtas` from the Parquet export
-- (default /mnt/zddb/export). Background: ~/org/20261112_oireachtas-data-report.org.
-- Safe to run again.
--
-- The vectors are text-embedding-3-small at 1536 dimensions, not the
-- text-embedding-3-large 1024 of dgi_vec, so a question must be embedded with
-- that model to search them. They are stored at full precision; the search
-- index (`load-oireachtas --vector-index`) is built on a half-precision copy,
-- which halves its size and loses nothing these values carry.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS dgi_oir;

-- Word-search text, weighted like Yes Deputy's Elasticsearch query (speaker^3,
-- answering_person^3, heading^2, asking_persons^2, then heading_path,
-- questions and the text): A = speaker and minister, B = heading and askers,
-- C = heading path and questions, D = the chunk text. Declared IMMUTABLE so
-- it can feed a generated column (array_to_string is only STABLE in general,
-- but it is fixed for text[]).
CREATE OR REPLACE FUNCTION dgi_oir.chunk_tsv(
    speaker text, answering_person text, heading text, asking_persons text[],
    heading_path text, questions text[], body text)
RETURNS tsvector
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN setweight(to_tsvector('english'::regconfig, concat_ws(' ', speaker, answering_person)), 'A')
    || setweight(to_tsvector('english'::regconfig, concat_ws(' ', heading, array_to_string(asking_persons, ' '))), 'B')
    || setweight(to_tsvector('english'::regconfig, concat_ws(' ', heading_path, array_to_string(questions, ' '))), 'C')
    || setweight(to_tsvector('english'::regconfig, body), 'D');

-- One row per embedded chunk (Elasticsearch document in oireachtas-chunks-v2).
CREATE TABLE IF NOT EXISTS dgi_oir.chunk (
    chunk_id          text PRIMARY KEY,   -- first 16 hex of sha256('record_id|chunk_index')
    record_id         text NOT NULL,      -- parent section: sha256('date|house|section_id'); = dgi_oir.answer.id for Q&A
    chunk_index       int NOT NULL,       -- position within the parent
    chunk_type        text NOT NULL,      -- speech, answer_text or table
    house             text,               -- Dáil, Seanad or Committee
    committee         text,               -- committee code; NULL for the Dáil and Seanad
    doc_type          text,               -- debate or writtens; NULL on the 1.5 million older Q&A chunks
    date              date,
    year              smallint,
    section_id        text,               -- AKN eId, e.g. dbsect_3
    section_type      text,               -- debate, questions, question, WrittenAnswers, statement, ...
    heading           text,
    heading_path      text,               -- nested headings joined with ' > '
    department_role   text,               -- Q&A only
    answering_person  text,               -- Q&A only
    answering_role    text,               -- Q&A only
    asking_persons    text[],             -- Q&A only
    questions         text[],             -- Q&A only; parallel to question_numbers, which can be shorter
    question_numbers  int[],              -- Q&A only
    speaker           text,               -- speech only
    speaker_role      text,               -- speech only
    speakers          text[],             -- speech only: everyone in a merged exchange
    table_caption     text,               -- table only
    table_headers     text[],             -- table only
    body              text NOT NULL,      -- exactly the text that was embedded
    model             text NOT NULL,      -- text-embedding-3-small
    embedding         vector(1536) NOT NULL,
    tsv               tsvector GENERATED ALWAYS AS (dgi_oir.chunk_tsv(
                          speaker, answering_person, heading, asking_persons,
                          heading_path, questions, body)) STORED
);

-- Tables made before 14 Nov had tsv over the text alone: switch them to the
-- weighted form. This rewrites the whole table once (about half an hour at
-- 7.8 million rows); afterwards the check finds chunk_tsv and does nothing.
DO $$
BEGIN
    IF NOT EXISTS (
          SELECT 1
            FROM pg_attrdef d
                 JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
           WHERE d.adrelid = 'dgi_oir.chunk'::regclass
             AND a.attname = 'tsv'
             AND pg_get_expr(d.adbin, d.adrelid) LIKE '%chunk_tsv%'
    ) THEN
        DROP INDEX IF EXISTS dgi_oir.chunk_tsv;
        ALTER TABLE dgi_oir.chunk ALTER COLUMN tsv SET EXPRESSION AS (dgi_oir.chunk_tsv(
            speaker, answering_person, heading, asking_persons,
            heading_path, questions, body));
    END IF;
END $$;

-- Whole answers (written and oral), one row per Q&A section. Only those
-- since February 2026 have come across so far; older ones are only as chunks.
CREATE TABLE IF NOT EXISTS dgi_oir.answer (
    id                text PRIMARY KEY,   -- sha256('date|house|section_id')
    date              date NOT NULL,
    year              smallint,
    house             text,
    heading           text,
    section_id        text,
    department_role   text,
    answering_person  text,
    answering_role    text,
    question_numbers  int[],
    questions         text[],
    answer_clean      text,
    doc_type          text,               -- writtens or debate (oral PQs)
    source_file       text NOT NULL
);

-- Which export files have been loaded, so a re-run only reads new ones.
CREATE TABLE IF NOT EXISTS dgi_oir.loaded_file (
    path        text PRIMARY KEY,
    size        bigint NOT NULL,
    mtime       timestamptz NOT NULL,
    rows_read   bigint NOT NULL,
    rows_added  bigint NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

-- Ranked word search (BM25, pg_textsearch; deploy/pg-textsearch-setup.sh
-- installs it). Words are lower-cased and English stopwords dropped, but not
-- stemmed: Yes Deputy's Elasticsearch analyzer didn't stem either, and the
-- 'english' stemmer turns housing into hous, which matches every "House".
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictname = 'english_nostem'
                   AND dictnamespace = 'dgi_oir'::regnamespace) THEN
        CREATE TEXT SEARCH DICTIONARY dgi_oir.english_nostem (TEMPLATE = simple, STOPWORDS = english);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'english_nostem'
                   AND cfgnamespace = 'dgi_oir'::regnamespace) THEN
        CREATE TEXT SEARCH CONFIGURATION dgi_oir.english_nostem (COPY = simple);
        ALTER TEXT SEARCH CONFIGURATION dgi_oir.english_nostem
            ALTER MAPPING FOR asciiword, word, numword, asciihword, hword, numhword,
                              hword_asciipart, hword_part, hword_numpart
            WITH dgi_oir.english_nostem;
    END IF;
END $$;

-- The text the BM25 index scores. Names three times and headings and askers
-- twice stand in for the Elasticsearch field boosts (speaker^3, heading^2, ...).
-- A query must use exactly this call for the index to be used:
--   ORDER BY dgi_oir.chunk_bm25_text(speaker, answering_person, heading,
--            asking_persons, heading_path, questions, body)
--            <@> to_bm25query('words', 'dgi_oir.chunk_bm25')
CREATE OR REPLACE FUNCTION dgi_oir.chunk_bm25_text(
    speaker text, answering_person text, heading text, asking_persons text[],
    heading_path text, questions text[], body text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN concat_ws(' ',
    speaker, speaker, speaker,
    answering_person, answering_person, answering_person,
    heading, heading,
    array_to_string(asking_persons, ' '), array_to_string(asking_persons, ' '),
    heading_path,
    array_to_string(questions, ' '),
    body);

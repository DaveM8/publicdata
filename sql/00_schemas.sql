-- dgi-explore: schemas and shared helpers (see ~/org/dgi-explore.org).
-- Safe to run again.

CREATE SCHEMA IF NOT EXISTS dgi_cat;    -- catalogue: publishers, datasets, resources
CREATE SCHEMA IF NOT EXISTS dgi_prof;   -- what each downloaded file really contains
CREATE SCHEMA IF NOT EXISTS dgi_ref;    -- reference keys: counties, LEAs, EDs, ...
CREATE SCHEMA IF NOT EXISTS dgi_link;   -- which columns are which keys; joinable datasets
CREATE SCHEMA IF NOT EXISTS dgi_data;   -- loaded tables: CSO values, CSVs
CREATE SCHEMA IF NOT EXISTS dgi_geo;    -- loaded geo layers (PostGIS, ITM)
CREATE SCHEMA IF NOT EXISTS dgi_vec;    -- embeddings
CREATE SCHEMA IF NOT EXISTS dgi_insight;
CREATE SCHEMA IF NOT EXISTS dgi_oir;    -- the Oireachtas record (debates, answers) with its vectors

-- Accent-, case- and quote-insensitive key for matching: 'Garda SÍOCHÁNA' -> 'garda siochana'.
-- Use it on both sides of a comparison; always display the original text.
-- The same rules as textio.search_key() in Python.
CREATE OR REPLACE FUNCTION dgi_cat.f_key(t text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(
         lower(public.unaccent(
           'public.unaccent'::regdictionary,
           translate(normalize(t, NFC), '’‘“”–—' || chr(160), '''''""-- '))),
         '\s+', ' ', 'g');

-- Every text value changed by the double-encoding repair, so fixes are never silent.
CREATE TABLE IF NOT EXISTS dgi_prof.text_fix (
    source     text NOT NULL,            -- 'catalogue', 'file', ...
    ref        text NOT NULL,            -- dataset/resource id or file path
    field      text,                     -- column or line number
    before     text NOT NULL,
    after      text NOT NULL,
    logged_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS text_fix_ref ON dgi_prof.text_fix (source, ref);

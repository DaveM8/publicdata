-- Question answering over the CSO tables (see ~/org/20261111_dgi-search-plan.org,
-- Phase 1). Applied by `dgi-explore ask-prepare`. Safe to run again.
--
-- dgi_cso holds one view per CSO table, with one named column per dimension,
-- so that a query reads naturally:
--
--     SELECT
--            county
--          , value
--       FROM dgi_cso.cna13
--      WHERE census_year = '2022'
--        AND sex = 'Both sexes';
--
-- The views are made by Python (ask/prepare.py), because their columns differ
-- from table to table. dgi_ask holds what the tools need to find and describe
-- them.

CREATE SCHEMA IF NOT EXISTS dgi_ask;
CREATE SCHEMA IF NOT EXISTS dgi_cso;

-- The unit of each statistic ("Number", "%", "Euro Million").
CREATE TABLE IF NOT EXISTS dgi_ask.cso_unit (
    cat_id    int PRIMARY KEY,           -- dgi_data.cso_cat, a STATISTIC category
    unit      text,
    decimals  int
);

-- One row per CSO table: what the search and describe tools read.
CREATE TABLE IF NOT EXISTS dgi_ask.cso_info (
    matrix_id   int PRIMARY KEY,
    matrix      text NOT NULL,           -- A0101, as the CSO names it
    view_name   text NOT NULL UNIQUE,    -- the view in dgi_cso
    label       text,
    updated     text,
    sha256      text NOT NULL,           -- the file the table was loaded from
    dataset_id  text,                    -- its dataset in dgi_cat, for the source link
    columns     jsonb NOT NULL,          -- [{name, label, role, n, sample}] in view order
    statistics  jsonb NOT NULL,          -- [{label, unit}]
    time_from   text,
    time_to     text,
    geo_level   text,                    -- county, la, ed, ... or NULL
    n_values    bigint,
    search_doc  text NOT NULL,
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', search_doc)) STORED
);
CREATE INDEX IF NOT EXISTS cso_info_tsv ON dgi_ask.cso_info USING gin (tsv);
CREATE INDEX IF NOT EXISTS cso_info_dataset ON dgi_ask.cso_info (dataset_id);

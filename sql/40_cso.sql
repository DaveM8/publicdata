-- CSO PxStat tables (JSON-stat) as one long table of values.
-- Loaded by `dgi-explore load-cso`. Adapted from ~/gis/census/jsonstat.sql:
-- the same decoding (a value's position in the flat array gives one category
-- per dimension, row-major), done in Python and stored compactly.

-- One row per CSO table (the newest file when the mirror has several).
CREATE TABLE IF NOT EXISTS dgi_data.cso_table (
    matrix_id   int PRIMARY KEY,
    matrix      text NOT NULL UNIQUE,     -- A0101
    label       text,
    updated     text,
    sha256      text NOT NULL,            -- the file loaded
    dim_ids     text[] NOT NULL,          -- dimension ids, in the order of cso_value.cat_ids
    dim_labels  text[] NOT NULL,
    geo_dim     int,                      -- position in dim_ids of the location dimension, if any
    time_dim    int,
    stat_dim    int,
    n_values    bigint,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

-- Every category of every dimension, once. The CSO reuses dimension ids
-- (classifications) across tables, so a category id means the same thing in
-- every table that uses it.
CREATE TABLE IF NOT EXISTS dgi_data.cso_cat (
    cat_id   int PRIMARY KEY,
    dim_id   text NOT NULL,
    code     text NOT NULL,
    label    text,
    UNIQUE (dim_id, code)
);

-- The values: about 370M rows.
CREATE TABLE IF NOT EXISTS dgi_data.cso_value (
    matrix_id  int NOT NULL,
    cat_ids    int[] NOT NULL,            -- one category per dimension, in cso_table.dim_ids order
    geo_cat    int,                       -- copies of the location, time and statistic
    time_cat   int,                       -- categories, for fast filtering
    stat_cat   int,
    value      double precision           -- NULL where the CSO publishes no value
);
-- Rows are loaded table by table, so a BRIN index finds a table's rows cheaply.
CREATE INDEX IF NOT EXISTS cso_value_matrix ON dgi_data.cso_value USING brin (matrix_id) WITH (pages_per_range = 32);
-- That holds only when one process loads. load-cso uses ten workers, whose rows
-- interleave on disk, so the BRIN index ended up matching three quarters of the
-- table for any one matrix (found 12 Nov 2026: 35 s to read a 96-value table).
-- The B-tree finds one table's rows exactly; about 2 GB.
CREATE INDEX IF NOT EXISTS cso_value_matrix_bt ON dgi_data.cso_value (matrix_id);

-- Which reference area a location category names (filled after loading).
CREATE TABLE IF NOT EXISTS dgi_data.cso_cat_area (
    cat_id  int NOT NULL,
    level   text NOT NULL,
    code    text NOT NULL,
    PRIMARY KEY (cat_id, level, code)
);

-- Any CSO table in long form with labels:
--   SELECT * FROM dgi_data.cso('A0101');
CREATE OR REPLACE FUNCTION dgi_data.cso(m text)
RETURNS TABLE (categories jsonb, value double precision)
LANGUAGE sql STABLE AS $$
  SELECT
         jsonb_object_agg(t.dim_labels[x.n], c.label ORDER BY x.n)
       , v.value
    FROM dgi_data.cso_table t
         JOIN dgi_data.cso_value v ON v.matrix_id = t.matrix_id
       , unnest(v.cat_ids) WITH ORDINALITY x(cat_id, n)
         JOIN dgi_data.cso_cat c ON c.cat_id = x.cat_id
   WHERE t.matrix = m
GROUP BY v.ctid, v.value
$$;

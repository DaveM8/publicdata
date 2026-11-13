-- Profiles: what each downloaded file really contains. Keyed by sha256, so a
-- file published under several resources is profiled once. Filled by
-- `dgi-explore profile`, incrementally.

CREATE TABLE IF NOT EXISTS dgi_prof.file (
    sha256        text PRIMARY KEY,
    path          text NOT NULL,          -- one copy, relative to the mirror
    bytes         bigint,
    real_type     text,                   -- from the first bytes: csv text, zip, pdf, html, json, ...
    kind          text,                   -- table, jsonstat, geo, raster, document, webpage, json, archive, binary, empty, error
    encoding      text,                   -- utf-8, ascii, cp1252, mixed, utf-16 (text files only)
    had_bom       boolean,
    mixed         boolean,
    nfc_changed   boolean,
    fixed_count   int,                    -- lines/values repaired from double encoding
    n_rows        bigint,
    n_cols        int,
    summary       jsonb,                  -- kind-specific details
    error         text,
    version       int NOT NULL,
    profiled_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS file_kind ON dgi_prof.file (kind);

-- Columns of tables (CSV, sheets) and dimensions of JSON-stat cubes.
CREATE TABLE IF NOT EXISTS dgi_prof.col (
    sha256          text NOT NULL REFERENCES dgi_prof.file ON DELETE CASCADE,
    part            text NOT NULL DEFAULT '',   -- zip member and/or sheet name
    ordinal         int NOT NULL,
    name            text,
    role            text,                       -- JSON-stat role: geo, time, metric; else NULL
    inferred_type   text,                       -- int, float, year, date, text, empty, mixed
    null_frac       real,
    n_distinct      int,
    distinct_capped boolean,                    -- n_distinct is a lower bound
    top_values      jsonb,                      -- [[value, count], ...]
    samples         text[],
    PRIMARY KEY (sha256, part, ordinal)
);

-- Geo layers: vector layers (GeoJSON, shapefile, GPKG, GDB, KML) and rasters.
CREATE TABLE IF NOT EXISTS dgi_prof.layer (
    sha256         text NOT NULL REFERENCES dgi_prof.file ON DELETE CASCADE,
    part           text NOT NULL DEFAULT '',    -- path inside a zip, '' for the file itself
    layer          text NOT NULL,
    gdal_path      text,                        -- what to hand GDAL to open it
    geom_type      text,                        -- Point, MultiPolygon, ..., raster
    feature_count  bigint,
    srid           int,                         -- EPSG code where GDAL knows it
    srs_name       text,
    extent         float8[],                    -- [minx, miny, maxx, maxy] in the layer's own CRS
    fields         jsonb,                       -- [[name, type], ...]
    sample         jsonb,                       -- a few attribute rows, text repaired
    encoding       text,                        -- for shapefiles: what was handed to GDAL, and why
    PRIMARY KEY (sha256, part, layer)
);

-- JSON-stat cubes (CSO PxStat tables).
CREATE TABLE IF NOT EXISTS dgi_prof.jsonstat (
    sha256      text PRIMARY KEY REFERENCES dgi_prof.file ON DELETE CASCADE,
    matrix      text,                     -- CSO table code, e.g. A0101
    label       text,
    updated     text,
    version     text,                     -- JSON-stat 1.0 or 2.0
    n_values    bigint,
    dims        jsonb,                    -- [{id, label, role, size, categories: {code: label} (first 2000)}]
    units       jsonb,
    notes       text[],
    extension   jsonb
);
CREATE INDEX IF NOT EXISTS jsonstat_matrix ON dgi_prof.jsonstat (matrix);

-- Readable text of documents and web pages, for search and embeddings.
CREATE TABLE IF NOT EXISTS dgi_prof.doc_text (
    sha256  text PRIMARY KEY REFERENCES dgi_prof.file ON DELETE CASCADE,
    title   text,
    chars   int,                          -- length of the full text; text may be cut at 200k
    text    text
);

-- Added 13 Oct: every distinct value of code- and name-like columns (not
-- measures), up to 20,000, so Phase 3 can recognise location and time columns.
ALTER TABLE dgi_prof.col ADD COLUMN IF NOT EXISTS distinct_values text[];

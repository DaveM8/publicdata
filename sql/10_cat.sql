-- Catalogue: publishers, datasets and resources from the mirror's state.sqlite
-- plus the DCAT JSON-LD catalogue pages. Reloaded in full by `dgi-explore catalog`.

CREATE TABLE IF NOT EXISTS dgi_cat.publisher (
    id        text PRIMARY KEY,          -- data.gov.ie organisation URI, or 'name:<name>'
    name      text NOT NULL,
    name_key  text GENERATED ALWAYS AS (dgi_cat.f_key(name)) STORED
);

CREATE TABLE IF NOT EXISTS dgi_cat.dataset (
    id                 text PRIMARY KEY, -- data.gov.ie dataset uuid
    uri                text,
    title              text,
    title_ga           text,             -- Irish title, where given
    description        text,
    publisher_id       text REFERENCES dgi_cat.publisher,
    themes             text[],           -- EU data-theme codes (HEAL, ECON, ...) and free labels
    keywords           text[],
    periodicity        text,
    created            timestamptz,
    issued             timestamptz,
    modified           timestamptz,
    temporal_start     date,
    temporal_end       date,
    spatial            geometry(Geometry, 4326),  -- footprint declared in the catalogue (few have one)
    languages          text[],
    licence            text,
    contact_email      text,
    conforms_to        text[],
    provenance         text,
    identifier         text,
    in_latest_catalogue boolean NOT NULL DEFAULT true,
    jsonld             jsonb,            -- the raw DCAT node, for anything not unpacked above
    search_doc         text,             -- f_key() of title, Irish title, description, keywords, publisher
    tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(search_doc, ''))) STORED
);
CREATE INDEX IF NOT EXISTS dataset_tsv ON dgi_cat.dataset USING gin (tsv);
CREATE INDEX IF NOT EXISTS dataset_title_trgm ON dgi_cat.dataset USING gin (dgi_cat.f_key(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS dataset_publisher ON dgi_cat.dataset (publisher_id);
CREATE INDEX IF NOT EXISTS dataset_spatial ON dgi_cat.dataset USING gist (spatial);

CREATE TABLE IF NOT EXISTS dgi_cat.resource (
    id            text PRIMARY KEY,      -- data.gov.ie resource uuid
    dataset_id    text NOT NULL REFERENCES dgi_cat.dataset,
    title         text,
    description   text,
    format        text,                  -- as the catalogue states it; dgi_prof has the real type
    media_type    text,
    url           text,
    host          text,
    kind          text,                  -- file, service, nourl
    status        text,                  -- done, pending, excluded, blocked, failed, ...
    path          text,                  -- relative to the mirror: files/<publisher>/<dataset>/<file>
    bytes         bigint,
    sha256        text,
    fetched_at    timestamptz,
    source        text,                  -- where the copy came from: direct, browser, wayback:<ts>
    licence       text,
    issued        timestamptz,
    modified      timestamptz
);
CREATE INDEX IF NOT EXISTS resource_dataset ON dgi_cat.resource (dataset_id);
CREATE INDEX IF NOT EXISTS resource_sha ON dgi_cat.resource (sha256);
CREATE INDEX IF NOT EXISTS resource_status ON dgi_cat.resource (status);

CREATE TABLE IF NOT EXISTS dgi_cat.load_log (
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    catalogue_dir text,
    datasets      int,
    resources     int,
    publishers    int,
    text_fixes    int
);

-- Lenient conversions for catalogue values: a malformed value becomes NULL
-- instead of aborting the whole load.
CREATE OR REPLACE FUNCTION dgi_cat.safe_ts(t text) RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN t::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION dgi_cat.safe_date(t text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN left(t, 10)::date;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION dgi_cat.safe_wkt(t text) RETURNS geometry
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE g geometry;
BEGIN
    IF t IS NULL OR t = '' THEN
        RETURN NULL;
    END IF;
    g := ST_SetSRID(ST_GeomFromText(regexp_replace(t, '^<[^>]*>\s*', '')), 4326);
    RETURN CASE WHEN ST_IsValid(g) THEN g ELSE ST_MakeValid(g) END;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

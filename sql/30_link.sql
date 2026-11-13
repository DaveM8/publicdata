-- Phase 3: which columns are location or time keys, which places each geo
-- layer covers, and from that, which datasets can be joined.
-- Rebuilt in full by `dgi-explore link` (a few minutes).

ALTER TABLE IF EXISTS dgi_link.layer_place ADD COLUMN IF NOT EXISTS share real;

-- ── time values ──────────────────────────────────────────────────────────────
-- Recognises the period formats in the mirror: 2020, 2020Q1 (CSO), 2020M01 (CSO),
-- 2020W05, 2020-03, 2020-03-15, 2019-2020, 2019/20, "March 2020", "Q1 2020".
CREATE OR REPLACE FUNCTION dgi_link.period(v text, OUT level text, OUT starts date, OUT ends date)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    t text := lower(btrim(v));
    m text[];
BEGIN
    IF t ~ '^(1[89]|20|21)\d{2}$' THEN
        level := 'year'; starts := make_date(t::int, 1, 1); ends := make_date(t::int, 12, 31);
        RETURN;
    END IF;
    m := coalesce(regexp_match(t, '^((?:19|20)\d{2})\s*q([1-4])$'),
                  (SELECT ARRAY[x[2], x[1]] FROM regexp_match(t, '^q([1-4])\s*((?:19|20)\d{2})$') x));
    IF m IS NOT NULL AND m[1] IS NOT NULL THEN
        level := 'quarter';
        starts := make_date(m[1]::int, m[2]::int * 3 - 2, 1);
        ends := (starts + interval '3 months - 1 day')::date;
        RETURN;
    END IF;
    m := coalesce(regexp_match(t, '^((?:19|20)\d{2})\s*m(\d{2})$'), regexp_match(t, '^((?:19|20)\d{2})-(\d{2})$'));
    IF m IS NOT NULL AND m[2]::int BETWEEN 1 AND 12 THEN
        level := 'month';
        starts := make_date(m[1]::int, m[2]::int, 1);
        ends := (starts + interval '1 month - 1 day')::date;
        RETURN;
    END IF;
    m := regexp_match(t, '^((?:19|20)\d{2})\s*w(\d{2})$');
    IF m IS NOT NULL AND m[2]::int BETWEEN 1 AND 53 THEN
        level := 'week';
        starts := to_date(m[1] || '-' || m[2], 'IYYY-IW');
        ends := starts + 6;
        RETURN;
    END IF;
    IF t ~ '^(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])' THEN
        level := 'date'; starts := left(t, 10)::date; ends := starts;
        RETURN;
    END IF;
    m := regexp_match(t, '^((?:19|20)\d{2})\s*[-/]\s*(\d{2}|\d{4})$');
    IF m IS NOT NULL THEN
        level := 'year_range';
        starts := make_date(m[1]::int, 1, 1);
        ends := make_date((CASE WHEN length(m[2]) = 2 THEN left(m[1], 2) || m[2] ELSE m[2] END)::int, 12, 31);
        RETURN;
    END IF;
    IF t ~ '^(january|february|march|april|may|june|july|august|september|october|november|december)\s+(19|20)\d{2}$' THEN
        level := 'month'; starts := to_date(t, 'fmmonth yyyy');
        ends := (starts + interval '1 month - 1 day')::date;
    END IF;
END $$;

-- Could this value be a period at all? A cheap test before calling period().
CREATE OR REPLACE FUNCTION dgi_link.maybe_period(v text)
RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN length(v) <= 25
   AND lower(btrim(v)) ~ '^(q[1-4]\s*)?(1[89]|20|21)\d{2}|^(january|february|march|april|may|june|july|august|september|october|november|december)\s';

-- ── matching a list of values against the reference areas ────────────────────
-- For each level: how many of the values (categories) name an area at that
-- level, and how many distinct areas. Each value is also tried without a
-- leading code and without a ", County" suffix, so CSO labels such as
-- "001 Abbey A, Limerick City" find the ED "Abbey A". Totals such as 'State'
-- or '-' are left out. `share` is the fraction of values naming an area at
-- ANY level: CSO dimensions often mix levels (State, provinces, counties).
DROP FUNCTION IF EXISTS dgi_link.geo_match(text[]);
CREATE OR REPLACE FUNCTION dgi_link.geo_match(vals text[])
RETURNS TABLE (level text, matched int, areas int, considered int, share real)
LANGUAGE sql STABLE AS $$
  WITH v AS (
    SELECT DISTINCT
           btrim(x) AS x
      FROM unnest(vals) x
     WHERE x IS NOT NULL
       AND btrim(x) NOT IN ('', '-', '_T', 'Total', 'All', 'State', 'Ireland', 'Unknown', 'Not stated')
  ),
  variants AS (
    SELECT
           v.x
         , dgi_ref.akey(y) AS k
      FROM v
         , unnest(ARRAY[
             v.x,
             regexp_replace(v.x, '^\d+\s+', ''),
             split_part(regexp_replace(v.x, '^\d+\s+', ''), ',', 1)
           ]) y
  ),
  hits AS (
    SELECT DISTINCT
           variants.x
         , a.level
         , a.code
      FROM variants
           JOIN dgi_ref.alias a ON a.akey = variants.k
     WHERE variants.k <> ''
  ),
  n AS (
    SELECT
           (SELECT count(*) FROM v)::int AS considered
         , (SELECT count(DISTINCT x) FROM hits)::int AS any_level
  )
    SELECT
           h.level
         , count(DISTINCT h.x)::int AS matched
         , count(DISTINCT h.code)::int AS areas
         , n.considered
         , (n.any_level::real / nullif(n.considered, 0))::real AS share
      FROM hits h
         , n
  GROUP BY h.level, n.considered, n.any_level
$$;

-- The level of a list of values: at least 70% of them name areas (at any
-- level), and the chosen level is the one naming the most values, with at
-- least 3 areas. So State + 4 provinces + 26 counties is 'county', and 26
-- county names stay 'county' rather than 'la'.
DROP FUNCTION IF EXISTS dgi_link.geo_level(text[]);
CREATE OR REPLACE FUNCTION dgi_link.geo_level(vals text[])
RETURNS TABLE (level text, matched int, considered int, share real)
LANGUAGE sql STABLE AS $$
    SELECT
           m.level
         , m.matched
         , m.considered
         , m.share
      FROM dgi_link.geo_match(vals) m
     WHERE m.share >= 0.7
       AND m.areas >= 3
  ORDER BY m.matched DESC
         , m.areas DESC
         , array_position(ARRAY['state', 'province', 'nuts2', 'nuts3', 'county', 'la', 'lea', 'ed', 'sa'], m.level)
     LIMIT 1
$$;

-- ── results ──────────────────────────────────────────────────────────────────

-- One row per CSO classification (dimension id such as C02779V03348), shared by
-- every CSO table that uses it: tables with the same classification line up exactly.
CREATE TABLE IF NOT EXISTS dgi_link.cso_class (
    dim_id        text PRIMARY KEY,
    label         text,
    size          int,
    n_tables      int,
    role          text,
    concept       text,          -- geo, time, or NULL (another breakdown: sex, age, sector, ...)
    level         text,          -- geo: state ... sa; time: year, quarter, month, ...
    matched       int,
    share         real,
    starts        date,
    ends          date,
    example_matrix text
);

-- Every location or time key column, in tables, sheets, CSO cubes and geo layers.
CREATE TABLE IF NOT EXISTS dgi_link.key_column (
    sha256        text NOT NULL,
    part          text NOT NULL DEFAULT '',
    ordinal       int,
    name          text,
    concept       text NOT NULL,  -- geo or time
    level         text NOT NULL,
    matched       int,
    share         real,
    starts        date,
    ends          date,
    cso_dim       text,           -- CSO classification, for exact joins between CSO tables
    PRIMARY KEY (sha256, part, ordinal, concept)
);

-- Which local authorities each geo layer's extent overlaps.
CREATE TABLE IF NOT EXISTS dgi_link.layer_place (
    sha256    text NOT NULL,
    part      text NOT NULL,
    layer     text NOT NULL,
    srid_used int,                -- the layer's own, or guessed from its coordinates
    la_code   text NOT NULL,
    share     real,               -- fraction of the layer's extent inside this council
                                  -- (an approximation until Phase 4 loads the features)
    PRIMARY KEY (sha256, part, layer, la_code)
);

-- Guess a layer's coordinate system from where its extent falls, when GDAL
-- doesn't know it (MapInfo files, some shapefiles without a .prj).
CREATE OR REPLACE FUNCTION dgi_link.guess_srid(e float8[])
RETURNS int
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
           WHEN e IS NULL OR array_length(e, 1) < 4 THEN NULL
           WHEN e[1] BETWEEN -11.5 AND -5 AND e[3] BETWEEN -11.5 AND -5
            AND e[2] BETWEEN 51 AND 55.5 AND e[4] BETWEEN 51 AND 55.5 THEN 4326
           WHEN e[1] BETWEEN 400000 AND 800000 AND e[2] BETWEEN 500000 AND 1000000 THEN 2157
           WHEN e[1] BETWEEN 0 AND 400000 AND e[2] BETWEEN 0 AND 500000 THEN 29902
           WHEN e[1] BETWEEN -1300000 AND -550000 AND e[2] BETWEEN 6600000 AND 7500000 THEN 3857
         END
$$;

-- A layer extent as a polygon in ITM, or NULL if the extent is unusable
-- (some files report infinite or out-of-range coordinates).
CREATE OR REPLACE FUNCTION dgi_link.extent_itm(e float8[], srid int)
RETURNS geometry
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF e IS NULL OR srid IS NULL OR array_length(e, 1) <> 4
       OR 'Infinity' = ANY(e) OR '-Infinity' = ANY(e) OR 'NaN' = ANY(e) THEN
        RETURN NULL;
    END IF;
    RETURN ST_Transform(ST_MakeEnvelope(e[1], e[2], e[3], e[4], srid), 2157);
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

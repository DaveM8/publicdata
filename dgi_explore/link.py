"""Phase 3: find the location and time keys in every profiled file.

1. CSO classifications (shared dimension ids) are matched once each against
   the reference areas, or parsed as time periods.
2. Every CSO cube's dimensions inherit their classification's result.
3. Table and sheet columns are matched from their distinct values.
4. Each geo layer's extent is placed on the local-authority map.
5. dgi_link.dataset_key rolls this up per dataset, which is what the
   browsers and the nugget finder use.
"""

import logging

from . import db

log = logging.getLogger("dgi")

STEPS = [
    ("CSO classifications", "cso_class", """
    INSERT INTO dgi_link.cso_class (dim_id, label, size, n_tables, role, example_matrix)
      SELECT
             d->>'id'
           , mode() WITHIN GROUP (ORDER BY d->>'label')
           , max((d->>'size')::int)
           , count(*)
           , max(d->>'role')
           , min(j.matrix)
        FROM dgi_prof.jsonstat j
           , jsonb_array_elements(j.dims) d
       WHERE d->>'id' <> 'STATISTIC'
    GROUP BY d->>'id'
    """),
    ("CSO classifications: time", None, """
    WITH p AS (
      SELECT
             d->>'id' AS dim_id
           , (dgi_link.period(c.key)).*
        FROM dgi_prof.jsonstat j
           , jsonb_array_elements(j.dims) d
           , jsonb_each_text(d->'categories') c
       WHERE d->>'role' = 'time'
          OR d->>'id' ~ '^TLIST'
    ),
    agg AS (
      SELECT
             dim_id
           , mode() WITHIN GROUP (ORDER BY level) AS level
           , min(starts) AS starts
           , max(ends) AS ends
           , count(level)::real / count(*) AS share
        FROM p
    GROUP BY dim_id
    )
    UPDATE dgi_link.cso_class k
       SET concept = 'time'
         , level = coalesce(a.level, 'period')
         , share = a.share
         , starts = a.starts
         , ends = a.ends
      FROM agg a
     WHERE k.dim_id = a.dim_id
       AND k.concept IS NULL
    """),
    ("key columns: CSO cubes, location (each table's own categories)", "key_column", """
    -- The CSO reuses a classification id for different category lists (44
    -- counties in one table, 3,741 EDs in another), so every table's dimension
    -- is matched on its own; labels and codes are tried, the better one wins.
    WITH dims AS (
      SELECT
             j.sha256
           , d.ordinality - 1 AS ordinal
           , d.value->>'label' AS name
           , d.value->>'id' AS dim_id
           , d.value->'categories' AS cats
        FROM dgi_prof.jsonstat j
           , jsonb_array_elements(j.dims) WITH ORDINALITY d
       WHERE d.value->>'id' <> 'STATISTIC'
         AND d.value->>'role' IS DISTINCT FROM 'time'
         AND d.value->>'id' !~ '^TLIST'
         AND (d.value->>'size')::int >= 3
    )
    INSERT INTO dgi_link.key_column (sha256, part, ordinal, name, concept, level, matched, share, cso_dim)
      SELECT
             dims.sha256
           , ''
           , dims.ordinal
           , dims.name
           , 'geo'
           , g.level
           , g.matched
           , g.share
           , dims.dim_id
        FROM dims
           , LATERAL (
               SELECT *
                 FROM (SELECT * FROM dgi_link.geo_level(ARRAY(SELECT value FROM jsonb_each_text(dims.cats)))
                        UNION ALL
                       SELECT * FROM dgi_link.geo_level(ARRAY(SELECT jsonb_object_keys(dims.cats)))
                      ) u
             ORDER BY matched DESC
                LIMIT 1
             ) g
    ON CONFLICT DO NOTHING
    """),
    ("CSO classifications: location summary", None, """
    UPDATE dgi_link.cso_class k
       SET concept = 'geo'
         , level = s.level
         , matched = s.tables
      FROM (
        SELECT
               cso_dim
             , mode() WITHIN GROUP (ORDER BY level) AS level
             , count(*)::int AS tables
          FROM dgi_link.key_column
         WHERE concept = 'geo'
           AND cso_dim IS NOT NULL
      GROUP BY cso_dim
           ) s
     WHERE k.dim_id = s.cso_dim
    """),
    ("key columns: CSO cubes, time (each table's own periods)", None, """
    WITH p AS (
      SELECT
             j.sha256
           , d.ordinality - 1 AS ordinal
           , d.value->>'label' AS name
           , d.value->>'id' AS dim_id
           , (dgi_link.period(c.key)).*
        FROM dgi_prof.jsonstat j
           , jsonb_array_elements(j.dims) WITH ORDINALITY d
           , jsonb_each_text(d.value->'categories') c
       WHERE d.value->>'role' = 'time'
          OR d.value->>'id' ~ '^TLIST'
    )
    INSERT INTO dgi_link.key_column (sha256, part, ordinal, name, concept, level, share,
                                     starts, ends, cso_dim)
      SELECT
             sha256
           , ''
           , ordinal
           , min(name)
           , 'time'
           , coalesce(mode() WITHIN GROUP (ORDER BY level), 'period')
           , count(level)::real / count(*)
           , min(starts)
           , max(ends)
           , min(dim_id)
        FROM p
    GROUP BY sha256, ordinal
    ON CONFLICT DO NOTHING
    """),
    ("key columns: tables, location", None, """
    INSERT INTO dgi_link.key_column (sha256, part, ordinal, name, concept, level, matched, share)
      SELECT
             c.sha256
           , c.part
           , c.ordinal
           , c.name
           , 'geo'
           , g.level
           , g.matched
           , g.share
        FROM dgi_prof.col c
           , LATERAL dgi_link.geo_level(c.distinct_values) g
       WHERE c.distinct_values IS NOT NULL
         AND c.role IS NULL
         AND c.inferred_type IN ('text', 'mixed', 'int')
         AND c.n_distinct BETWEEN 1 AND 20000
    ON CONFLICT DO NOTHING
    """),
    ("key columns: tables, time", None, """
    WITH p AS (
      SELECT
             c.sha256
           , c.part
           , c.ordinal
           , c.name
           , (dgi_link.period(v)).*
        FROM dgi_prof.col c
           , unnest(c.distinct_values) v
       WHERE c.distinct_values IS NOT NULL
         AND c.role IS NULL
         AND c.inferred_type IN ('year', 'date', 'text', 'mixed')
         AND dgi_link.maybe_period(c.distinct_values[1])
    )
    INSERT INTO dgi_link.key_column (sha256, part, ordinal, name, concept, level, share, starts, ends)
      SELECT
             sha256
           , part
           , ordinal
           , min(name)
           , 'time'
           , mode() WITHIN GROUP (ORDER BY level)
           , count(level)::real / count(*)
           , min(starts)
           , max(ends)
        FROM p
    GROUP BY sha256, part, ordinal
      HAVING count(level)::real / count(*) >= 0.8
    ON CONFLICT DO NOTHING
    """),
    ("geo layers: places covered", "layer_place", """
    WITH box AS (
      SELECT
             l.sha256
           , l.part
           , l.layer
           , coalesce(l.srid, dgi_link.guess_srid(l.extent)) AS srid
           , dgi_link.extent_itm(l.extent, coalesce(l.srid, dgi_link.guess_srid(l.extent))) AS geom
        FROM dgi_prof.layer l
    )
    , hit AS (
      SELECT
             b.sha256
           , b.part
           , b.layer
           , b.srid
           , a.code
           , CASE WHEN ST_Area(b.geom) > 0
                  THEN sum(ST_Area(ST_ClipByBox2D(a.geom, b.geom))) / ST_Area(b.geom)
                  ELSE 1 END AS share
        FROM box b
             JOIN dgi_ref.la_sub a ON ST_Intersects(a.geom, b.geom)
       WHERE b.geom IS NOT NULL
    GROUP BY b.sha256, b.part, b.layer, b.srid, a.code, b.geom
    )
    -- Keep a council if it holds at least 15% of the extent, and always the
    -- one holding the most, so a layer is never left without a place.
    INSERT INTO dgi_link.layer_place (sha256, part, layer, srid_used, la_code, share)
      SELECT
             sha256
           , part
           , layer
           , srid
           , code
           , share
        FROM (
          SELECT
                 hit.*
               , rank() OVER (PARTITION BY sha256, part, layer ORDER BY share DESC) AS r
            FROM hit
             ) x
       WHERE share >= 0.15 OR r = 1
    ON CONFLICT DO NOTHING
    """),
]


def run(dry_run=False, step=None):
    """Run every step, or only those whose name contains `step` (e.g. "geo layers").

    A step that starts a table (cso_class, key_column, layer_place) empties it
    first; the steps after it add to it. Running a later step alone therefore
    only makes sense for "geo layers", which owns layer_place by itself.
    """
    with db.connect() as conn:
        db.run_sql_files(conn, "30_link")
        for name, table, sql in STEPS:
            if step and step.lower() not in name.lower():
                continue
            if dry_run:
                log.info("would run: %s", name)
                continue
            if table:
                conn.execute(f"TRUNCATE dgi_link.{table}")
            cur = conn.execute(sql)
            log.info("%s: %s rows", name, cur.rowcount)
        if dry_run:
            conn.rollback()
            return
        db.run_sql_files(conn, "35_link_views")
        conn.commit()
    log.info("links built")

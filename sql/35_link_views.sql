-- Per-dataset rollups of the key columns, and "what can this join with?".
-- Refreshed at the end of `dgi-explore link`.

DROP MATERIALIZED VIEW IF EXISTS dgi_link.dataset_key, dgi_link.dataset_place CASCADE;

-- Each dataset's location and time keys, from any of its files.
CREATE MATERIALIZED VIEW dgi_link.dataset_key AS
  SELECT
         r.dataset_id
       , k.concept
       , k.level
       , min(k.starts) AS starts
       , max(k.ends) AS ends
       , array_agg(DISTINCT k.cso_dim) FILTER (WHERE k.cso_dim IS NOT NULL) AS cso_dims
       , count(DISTINCT k.sha256) AS files
    FROM dgi_link.key_column k
         JOIN dgi_cat.resource r ON r.sha256 = k.sha256
GROUP BY r.dataset_id, k.concept, k.level;

CREATE INDEX dataset_key_level ON dgi_link.dataset_key (concept, level);
CREATE INDEX dataset_key_dataset ON dgi_link.dataset_key (dataset_id);

-- Which local authorities each dataset's geo layers cover.
CREATE MATERIALIZED VIEW dgi_link.dataset_place AS
  SELECT DISTINCT
         r.dataset_id
       , p.la_code
    FROM dgi_link.layer_place p
         JOIN dgi_cat.resource r ON r.sha256 = p.sha256;

CREATE INDEX dataset_place_la ON dgi_link.dataset_place (la_code);
CREATE INDEX dataset_place_dataset ON dgi_link.dataset_place (dataset_id);

-- Datasets that line up with a given one, best first:
--   same_cso_dim    CSO tables sharing a geographic classification: exact join
--   same_geo_level  location columns at the same level (county, LA, ED, ...)
--   same_places     geo layers covering the same local authorities
-- `years` is how many years their time ranges overlap (NULL if either has none).
CREATE OR REPLACE FUNCTION dgi_link.joinable_with(did text, max_rows int DEFAULT 100)
RETURNS TABLE (dataset_id text, title text, how text, level text, years int, score real)
LANGUAGE sql STABLE AS $$
  WITH me_geo AS (
    SELECT * FROM dgi_link.dataset_key WHERE dataset_id = did AND concept = 'geo'
  ),
  me_time AS (
    SELECT min(starts) AS starts, max(ends) AS ends
      FROM dgi_link.dataset_key WHERE dataset_id = did AND concept = 'time'
  ),
  cand AS (
    SELECT
           o.dataset_id
         , CASE WHEN o.cso_dims && m.cso_dims THEN 'same_cso_dim' ELSE 'same_geo_level' END AS how
         , o.level
      FROM me_geo m
           JOIN dgi_link.dataset_key o ON o.concept = 'geo' AND o.level = m.level
     WHERE o.dataset_id <> did
     UNION ALL
    SELECT
           o.dataset_id
         , 'same_places'
         , 'la'
      FROM dgi_link.dataset_place m
           JOIN dgi_link.dataset_place o ON o.la_code = m.la_code
     WHERE m.dataset_id = did
       AND o.dataset_id <> did
  GROUP BY o.dataset_id
    HAVING count(*) >= (SELECT count(*) * 0.5 FROM dgi_link.dataset_place WHERE dataset_id = did)
  ),
  scored AS (
    SELECT
           c.dataset_id
         , c.how
         , c.level
         , CASE
             WHEN t.starts IS NULL OR me.starts IS NULL THEN NULL
             ELSE greatest(0, extract(year FROM least(t.ends, me.ends))
                              - extract(year FROM greatest(t.starts, me.starts)) + 1)::int
           END AS years
      FROM cand c
           CROSS JOIN me_time me
           LEFT JOIN LATERAL (
             SELECT min(starts) AS starts, max(ends) AS ends
               FROM dgi_link.dataset_key
              WHERE dataset_id = c.dataset_id AND concept = 'time'
           ) t ON true
  )
    SELECT *
      FROM (
    SELECT DISTINCT ON (s.dataset_id)
           s.dataset_id
         , d.title
         , s.how
         , s.level
         , s.years
         , (CASE s.how WHEN 'same_cso_dim' THEN 3 WHEN 'same_geo_level' THEN 2 ELSE 1 END
            + coalesce(least(s.years, 10) / 10.0, 0))::real AS score
      FROM scored s
           JOIN dgi_cat.dataset d ON d.id = s.dataset_id
     WHERE s.years IS DISTINCT FROM 0
  ORDER BY s.dataset_id, score DESC
           ) best
  ORDER BY score DESC, title
     LIMIT max_rows
$$;

-- Where a dataset's data is in Postgres, and how to query it:
--   SELECT * FROM dgi_cat.tables_for('<dataset id from the page URL>');
CREATE OR REPLACE FUNCTION dgi_cat.tables_for(did text)
RETURNS TABLE (file text, kind text, query text, rows bigint, note text)
LANGUAGE sql STABLE AS $$
    SELECT
           array_length(v.files, 1) || ' file(s) stacked'
         , 'whole dataset (view)'
         , format('SELECT * FROM dgi_data.%I LIMIT 100;', v.view_name)
         , v.rows
         , 'source_file column says which file each row came from'
      FROM dgi_data.dataset_view v
     WHERE v.dataset_id = did
     UNION ALL
    SELECT
           r.title
         , 'CSO table'
         , format('SELECT * FROM dgi_data.cso(%L);', t.matrix)
         , t.n_values
         , t.label
      FROM dgi_cat.resource r
           JOIN dgi_data.cso_table t ON t.sha256 = r.sha256
     WHERE r.dataset_id = did
     UNION ALL
    SELECT
           coalesce(nullif(c.member, ''), r.title)
         , CASE WHEN c.member <> '' THEN 'CSV inside zip' ELSE 'CSV table' END
         , format('SELECT * FROM dgi_data.%I LIMIT 100;', c.table_name)
         , c.rows
         , CASE WHEN c.typed THEN 'number columns typed' ELSE 'all columns text' END
      FROM dgi_cat.resource r
           JOIN dgi_data.csv_table c ON c.sha256 = r.sha256 AND c.error IS NULL
     WHERE r.dataset_id = did
     UNION ALL
    SELECT
           r.title
         , 'map layer'
         , format('SELECT * FROM dgi_geo.%I LIMIT 100;', g.table_name)
         , g.features
         , g.layer || ' (' || coalesce(g.geom_type, '?') || ', ITM / EPSG:2157)'
      FROM dgi_cat.resource r
           JOIN dgi_geo.layer_table g ON g.sha256 = r.sha256 AND g.error IS NULL
     WHERE r.dataset_id = did
     UNION ALL
    SELECT
           r.title
         , 'not loaded: ' || coalesce(f.kind, r.status)
         , '/geo/mirror/' || substr(r.path, 7)
         , NULL
         , 'the original file; see its page for columns'
      FROM dgi_cat.resource r
           LEFT JOIN dgi_prof.file f ON f.sha256 = r.sha256
     WHERE r.dataset_id = did
       AND r.path IS NOT NULL
       AND NOT EXISTS (SELECT FROM dgi_data.cso_table t WHERE t.sha256 = r.sha256)
       AND NOT EXISTS (SELECT FROM dgi_data.csv_table c WHERE c.sha256 = r.sha256 AND c.error IS NULL)
       AND NOT EXISTS (SELECT FROM dgi_geo.layer_table g WHERE g.sha256 = r.sha256 AND g.error IS NULL)
$$;
GRANT EXECUTE ON FUNCTION dgi_cat.tables_for(text) TO PUBLIC;

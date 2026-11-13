"""Phase 6b: a SQLite copy of the catalogue for Datasette (~/dgi-explore/catalog.db).

Tables: datasets, files, columns, map_layers, cso_tables, similar. Full-text
search on datasets, columns and map layers; facets set up in
datasette-metadata.json (publisher, themes, location levels, kinds).

Served by the user service dgi-datasette on 127.0.0.1:8001 at /datasette/.
Datasette is read-only here; the export is rebuilt from Postgres.
"""

import json
import logging
from pathlib import Path

import sqlite_utils

from . import db

log = logging.getLogger("dgi")

OUT = Path(__file__).resolve().parent.parent / "catalog.db"
METADATA = OUT.with_name("datasette-metadata.json")


def export():
    tmp = OUT.with_suffix(".db.new")
    tmp.unlink(missing_ok=True)
    sq = sqlite_utils.Database(tmp)
    with db.connect() as conn:
        q = lambda s: conn.execute(s)

        def put(name, sql, pk=None, fts=None):
            cur = q(sql)
            cols = [c.name for c in cur.description]
            rows = (dict(zip(cols, r)) for r in cur)
            sq[name].insert_all(rows, pk=pk, batch_size=2000)
            if fts:
                sq[name].enable_fts(fts, create_triggers=False)
            log.info("%s: %d rows", name, sq[name].count)

        put("datasets", """
            SELECT d.id, d.title, d.title_ga, p.name AS publisher, d.description,
                   array_to_string(d.themes, ', ') AS themes, array_to_string(d.keywords, ', ') AS keywords,
                   (SELECT string_agg(DISTINCT k.level, ', ' ORDER BY k.level) FROM dgi_link.dataset_key k
                     WHERE k.dataset_id = d.id AND k.concept = 'geo') AS location_levels,
                   (SELECT min(extract(year FROM k.starts))::int FROM dgi_link.dataset_key k
                     WHERE k.dataset_id = d.id AND k.concept = 'time') AS from_year,
                   (SELECT max(extract(year FROM k.ends))::int FROM dgi_link.dataset_key k
                     WHERE k.dataset_id = d.id AND k.concept = 'time') AS to_year,
                   d.periodicity, d.licence, d.contact_email,
                   'https://data.gov.ie/dataset/' || d.id AS on_data_gov_ie,
                   '/dgi/d/' || d.id || '.html' AS page
              FROM dgi_cat.dataset d LEFT JOIN dgi_cat.publisher p ON p.id = d.publisher_id
            """, pk="id", fts=["title", "title_ga", "description", "keywords", "publisher"])
        put("files", """
            SELECT r.id, r.dataset_id, r.title, r.format AS stated_format, f.kind AS really_is,
                   f.real_type, f.encoding, f.fixed_count AS repaired_values, f.n_rows, f.n_cols,
                   r.bytes, r.status, f.error, '/geo/mirror/' || substr(r.path, 7) AS download
              FROM dgi_cat.resource r LEFT JOIN dgi_prof.file f ON f.sha256 = r.sha256
            """, pk="id")
        put("columns", """
            SELECT r.dataset_id, c.sha256, c.part, c.ordinal, c.name, c.role, c.inferred_type,
                   c.n_distinct, array_to_string(c.samples, ' | ') AS samples
              FROM dgi_prof.col c
                   JOIN LATERAL (SELECT dataset_id FROM dgi_cat.resource WHERE sha256 = c.sha256 LIMIT 1) r ON true
            """, fts=["name", "samples"])
        put("map_layers", """
            SELECT r.dataset_id, l.sha256, l.part, l.layer, l.geom_type, l.feature_count, l.srid,
                   (SELECT string_agg(f->>0, ', ') FROM jsonb_array_elements(l.fields) f) AS fields,
                   t.table_name AS postgis_table,
                   (SELECT string_agg(e.la_code, ', ' ORDER BY e.share DESC) FROM dgi_link.layer_place_exact e
                     WHERE e.table_name = t.table_name AND e.share >= 0.05) AS councils
              FROM dgi_prof.layer l
                   LEFT JOIN dgi_geo.layer_table t USING (sha256, part, layer)
                   JOIN LATERAL (SELECT dataset_id FROM dgi_cat.resource WHERE sha256 = l.sha256 LIMIT 1) r ON true
             WHERE l.geom_type IS NOT NULL
            """, fts=["layer", "fields"])
        put("cso_tables", """
            SELECT t.matrix, t.label, t.updated, t.n_values,
                   array_to_string(t.dim_labels, ', ') AS dimensions,
                   (SELECT r.dataset_id FROM dgi_cat.resource r WHERE r.sha256 = t.sha256 LIMIT 1) AS dataset_id
              FROM dgi_data.cso_table t
            """, pk="matrix", fts=["label", "dimensions"])
        put("similar", """
            SELECT s.dataset_id, s.rank, s.other_id, o.title AS other_title, s.score
              FROM dgi_vec.similar s JOIN dgi_cat.dataset o ON o.id = s.other_id
            """)
    for t, cols in {"files": ["dataset_id"], "columns": ["dataset_id"], "map_layers": ["dataset_id"],
                    "similar": ["dataset_id"]}.items():
        sq[t].create_index(cols, if_not_exists=True)
    sq.vacuum()
    tmp.replace(OUT)
    METADATA.write_text(json.dumps({
        "title": "data.gov.ie mirror catalogue",
        "description": "Every dataset, file, column, map layer and CSO table in the mirror. "
                       "Text is shown as published (Irish characters preserved).",
        "databases": {"catalog": {"tables": {
            "datasets": {"facets": ["publisher", "location_levels", "themes"], "sort": "title"},
            "files": {"facets": ["really_is", "stated_format", "encoding", "status"]},
            "columns": {"facets": ["inferred_type", "role"]},
            "map_layers": {"facets": ["geom_type", "councils"]},
        }}},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s", OUT)

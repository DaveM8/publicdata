"""Phase 4a: load CSO JSON-stat tables into dgi_data.

Steps:
1. Pick one file per CSO table (matrix): the most recently updated.
2. Build the category dictionary dgi_data.cso_cat from the profiles.
3. Decode each table's values in parallel worker processes, each with its own
   COPY into dgi_data.cso_value.
4. Map location categories to reference areas (dgi_data.cso_cat_area).

Incremental: a table is reloaded only when its chosen file (sha256) changed.
"""

import json
import logging
from multiprocessing import Pool

import psycopg

from . import db, textio

log = logging.getLogger("dgi")

_cats = None   # (dim_id, code) -> cat_id, loaded once per worker


def _worker_init():
    global _cats
    with db.connect() as conn:
        _cats = {(d, c): i for i, d, c in conn.execute("SELECT cat_id, dim_id, code FROM dgi_data.cso_cat")}


def _decode(doc):
    """(dim ids, dim labels, category codes per dim, roles, values as {position: value})."""
    if doc.get("class") != "dataset":
        doc = doc.get("dataset") or next(iter(doc.values()))
    dim = doc.get("dimension", {})
    ids = doc.get("id") or dim.get("id") or []
    sizes = doc.get("size") or dim.get("size") or []
    role = doc.get("role") or dim.get("role") or {}
    codes = []
    for d in ids:
        index = dim[d].get("category", {}).get("index")
        if isinstance(index, list):
            codes.append(index)
        elif isinstance(index, dict):
            codes.append([c for c, _ in sorted(index.items(), key=lambda kv: kv[1])])
        else:
            codes.append(list(dim[d].get("category", {}).get("label", {})))
    labels = [dim[d].get("label") or d for d in ids]
    values = doc.get("value")
    if isinstance(values, dict):
        values = {int(k): v for k, v in values.items()}
    elif values is None:
        values = {}
    return ids, labels, codes, sizes, role, values


def load_one(task):
    """Decode one table and COPY its values. Returns (matrix_id, n_values, error)."""
    matrix_id, sha256, path = task
    try:
        doc = json.loads(textio.read_text(db.MIRROR / path).text)
        ids, labels, codes, sizes, role, values = _decode(doc)
        cat_lists = [[_cats.get((d, c)) for c in cs] for d, cs in zip(ids, codes)]
        strides = []
        acc = 1
        for s in reversed(sizes):
            strides.append(acc)
            acc *= s
        strides.reverse()
        time_ids = set(role.get("time", [])) | {d for d in ids if d.startswith("TLIST")}
        geo_pos = None
        time_pos = next((i for i, d in enumerate(ids) if d in time_ids), None)
        stat_pos = next((i for i, d in enumerate(ids) if d == "STATISTIC" or d in role.get("metric", [])), None)
        with db.connect() as conn:
            row = conn.execute("SELECT geo_dim FROM dgi_data.cso_table WHERE matrix_id = %s", (matrix_id,)).fetchone()
            geo_pos = row[0] if row else None
            with conn.cursor().copy("COPY dgi_data.cso_value (matrix_id, cat_ids, geo_cat, time_cat, stat_cat, value) "
                                    "FROM STDIN (FORMAT BINARY)") as cp:
                cp.set_types(["int4", "int4[]", "int4", "int4", "int4", "float8"])
                items = enumerate(values) if isinstance(values, list) else sorted(values.items())
                n = 0
                for pos, v in items:
                    if v is not None and not isinstance(v, (int, float)):
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            v = None
                    cats = [cat_lists[k][(pos // strides[k]) % sizes[k]] for k in range(len(sizes))]
                    cp.write_row((matrix_id, cats,
                                  cats[geo_pos] if geo_pos is not None else None,
                                  cats[time_pos] if time_pos is not None else None,
                                  cats[stat_pos] if stat_pos is not None else None,
                                  v))
                    n += 1
            conn.execute("UPDATE dgi_data.cso_table SET n_values = %s, time_dim = %s, stat_dim = %s, "
                         "loaded_at = now() WHERE matrix_id = %s", (n, time_pos, stat_pos, matrix_id))
        return matrix_id, n, None
    except Exception as e:
        return matrix_id, 0, f"{type(e).__name__}: {e}"[:500]


def prepare(conn):
    """Choose files, refresh cso_table and cso_cat. Returns the tables to (re)load."""
    db.run_sql_files(conn, "40_cso")
    # The newest file per matrix (by its own 'updated' stamp, then by file).
    conn.execute("""
        CREATE TEMP TABLE pick AS
          SELECT DISTINCT ON (j.matrix)
                 j.matrix
               , j.sha256
               , j.label
               , j.updated
               , j.dims
               , f.path
            FROM dgi_prof.jsonstat j
                 JOIN dgi_prof.file f USING (sha256)
           WHERE j.matrix IS NOT NULL AND j.matrix <> ''
        ORDER BY j.matrix, j.updated DESC NULLS LAST, f.path
    """)
    # Categories: every (dimension, code) seen; keep existing ids stable.
    conn.execute("""
        INSERT INTO dgi_data.cso_cat (cat_id, dim_id, code, label)
          SELECT
                 (SELECT coalesce(max(cat_id), 0) FROM dgi_data.cso_cat) + row_number() OVER ()
               , n.dim_id
               , n.code
               , n.label
            FROM (
              SELECT DISTINCT ON (d->>'id', c.key)
                     d->>'id' AS dim_id
                   , c.key AS code
                   , c.value AS label
                FROM pick p
                   , jsonb_array_elements(p.dims) d
                   , jsonb_each_text(d->'categories') c
            ORDER BY d->>'id', c.key
                 ) n
           WHERE NOT EXISTS (SELECT FROM dgi_data.cso_cat k WHERE k.dim_id = n.dim_id AND k.code = n.code)
    """)
    # Tables whose file changed: drop their old values and reload. (A table
    # whose load failed has n_values NULL and no rows: COPY is all or nothing.)
    conn.execute("""
        CREATE TEMP TABLE stale AS
          SELECT t.matrix_id
            FROM dgi_data.cso_table t
                 JOIN pick p USING (matrix)
           WHERE t.sha256 <> p.sha256
    """)
    conn.execute("DELETE FROM dgi_data.cso_value v USING stale s WHERE v.matrix_id = s.matrix_id")
    conn.execute("DELETE FROM dgi_data.cso_table t USING stale s WHERE t.matrix_id = s.matrix_id")
    conn.execute("""
        INSERT INTO dgi_data.cso_table (matrix_id, matrix, label, updated, sha256, dim_ids, dim_labels, geo_dim)
          SELECT
                 (SELECT coalesce(max(matrix_id), 0) FROM dgi_data.cso_table) + row_number() OVER (ORDER BY p.matrix)
               , p.matrix
               , p.label
               , p.updated
               , p.sha256
               , ARRAY(SELECT d->>'id' FROM jsonb_array_elements(p.dims) d)
               , ARRAY(SELECT coalesce(d->>'label', d->>'id') FROM jsonb_array_elements(p.dims) d)
               , (SELECT k.ordinal FROM dgi_link.key_column k
                   WHERE k.sha256 = p.sha256 AND k.concept = 'geo' ORDER BY k.matched DESC LIMIT 1)
            FROM pick p
           WHERE NOT EXISTS (SELECT FROM dgi_data.cso_table t WHERE t.matrix = p.matrix)
    """)
    todo = conn.execute("""
        SELECT t.matrix_id, t.sha256, p.path
          FROM dgi_data.cso_table t
               JOIN pick p USING (matrix)
         WHERE t.n_values IS NULL
      ORDER BY t.matrix_id
    """).fetchall()
    conn.commit()
    return todo


def map_areas(conn):
    """Location categories → reference areas, for the dimensions found to be locations."""
    conn.execute("TRUNCATE dgi_data.cso_cat_area")
    conn.execute("""
        INSERT INTO dgi_data.cso_cat_area (cat_id, level, code)
          SELECT DISTINCT
                 c.cat_id
               , a.level
               , a.code
            FROM dgi_data.cso_cat c
                 JOIN (SELECT DISTINCT cso_dim, level FROM dgi_link.key_column
                        WHERE concept = 'geo' AND cso_dim IS NOT NULL) k ON k.cso_dim = c.dim_id
               , unnest(ARRAY[c.label, c.code,
                              regexp_replace(c.label, '^\\d+\\s+', ''),
                              split_part(regexp_replace(c.label, '^\\d+\\s+', ''), ',', 1)]) v
                 JOIN dgi_ref.alias a ON a.akey = dgi_ref.akey(v)
    """)
    conn.commit()


def run(dry_run=False, workers=10, limit=None):
    with db.connect() as conn:
        todo = prepare(conn) if not dry_run else []
        if dry_run:
            log.info("dry run: nothing loaded")
            return
        if limit:
            todo = todo[:limit]
        log.info("%d CSO tables to load", len(todo))
        done = errors = values = 0
        with Pool(workers, initializer=_worker_init) as pool:
            for matrix_id, n, err in pool.imap_unordered(load_one, todo, chunksize=8):
                done += 1
                values += n
                if err:
                    errors += 1
                    log.warning("matrix_id %s: %s", matrix_id, err)
                if done % 500 == 0:
                    log.info("loaded %d/%d tables, %d values, %d errors", done, len(todo), values, errors)
        log.info("loaded %d tables, %d values, %d errors", done, values, errors)
        map_areas(conn)
        conn.execute("ANALYZE dgi_data.cso_value")
        conn.commit()
        size = conn.execute("SELECT pg_size_pretty(pg_total_relation_size('dgi_data.cso_value'))").fetchone()[0]
        log.info("dgi_data.cso_value is now %s", size)

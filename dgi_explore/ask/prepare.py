"""ask-prepare: make the CSO tables easy to query.

Builds one view per CSO table in schema dgi_cso, with a named column per
dimension plus value and unit, and fills dgi_ask.cso_info (what the search and
describe tools read) and dgi_ask.cso_unit. Rebuilds everything; takes a minute
or two. Run it after load-cso.

Plan: ~/org/20261111_dgi-search-plan.org, Phase 1, "the statistics layer".
"""

import json
import logging
import re
import unicodedata

from psycopg import sql

from .. import db

log = logging.getLogger("dgi")

BATCH = 200          # views per transaction (each takes locks)
SAMPLE = 12          # category labels kept per column in cso_info
RESERVED = {"value", "unit"}


def ident(label, used):
    """A plain column or view name from a label: 'Census Year' -> census_year."""
    s = unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:50] or "col"
    if s[0].isdigit():
        s = "c_" + s
    base, n = s, 2
    while s in used or s in RESERVED:
        s = f"{base}_{n}"
        n += 1
    used.add(s)
    return s


def view_sql(view, matrix_id, cols):
    """CREATE VIEW for one table. cols: column names in dimension order."""
    select = [sql.SQL("c{}.label AS {}").format(sql.SQL(str(i)), sql.Identifier(c))
              for i, c in enumerate(cols, 1)]
    joins = [sql.SQL("LEFT JOIN dgi_data.cso_cat c{i} ON c{i}.cat_id = v.cat_ids[{i}]")
             .format(i=sql.SQL(str(i))) for i in range(1, len(cols) + 1)]
    return sql.SQL(
        "CREATE VIEW dgi_cso.{view} AS\n"
        "  SELECT\n         {select}\n       , v.value\n       , u.unit\n"
        "    FROM dgi_data.cso_value v\n         {joins}\n"
        "         LEFT JOIN dgi_ask.cso_unit u ON u.cat_id = v.stat_cat\n"
        "   WHERE v.matrix_id = {mid}"
    ).format(view=sql.Identifier(view),
             select=sql.SQL("\n       , ").join(select),
             joins=sql.SQL("\n         ").join(joins),
             mid=sql.Literal(matrix_id))


def run():
    with db.connect() as conn:
        db.run_sql_files(conn, "45_ask")
        conn.commit()

        # Units of the statistics, from the JSON-stat profiles.
        conn.execute("TRUNCATE dgi_ask.cso_unit")
        conn.execute("""
            INSERT INTO dgi_ask.cso_unit (cat_id, unit, decimals)
              SELECT DISTINCT ON (c.cat_id)
                     c.cat_id
                   , u.value->>'label'
                   , (u.value->>'decimals')::int
                FROM dgi_data.cso_table t
                     JOIN dgi_prof.jsonstat j USING (sha256)
                     CROSS JOIN LATERAL jsonb_each(j.units) u
                     JOIN dgi_data.cso_cat c ON c.dim_id = t.dim_ids[t.stat_dim + 1]
                                            AND c.code = u.key
               WHERE t.stat_dim IS NOT NULL
                 AND jsonb_typeof(j.units) = 'object'
                 AND jsonb_typeof(u.value) = 'object'
            ORDER BY c.cat_id
        """)
        conn.commit()

        tables = conn.execute("""
            SELECT
                   t.matrix_id
                 , t.matrix
                 , t.label
                 , t.updated
                 , t.sha256
                 , t.dim_labels
                 , t.geo_dim
                 , t.time_dim
                 , t.stat_dim
                 , t.n_values
                 , j.dims
                 , j.units
                 , (SELECT r.dataset_id
                      FROM dgi_cat.resource r
                     WHERE r.sha256 = t.sha256
                  ORDER BY r.dataset_id
                     LIMIT 1) AS dataset_id
                 , (SELECT k.level
                      FROM dgi_link.key_column k
                     WHERE k.sha256 = t.sha256
                       AND k.concept = 'geo'
                  ORDER BY k.matched DESC NULLS LAST
                     LIMIT 1) AS geo_level
              FROM dgi_data.cso_table t
                   JOIN dgi_prof.jsonstat j USING (sha256)
             WHERE t.n_values IS NOT NULL
          ORDER BY t.matrix
        """).fetchall()
        log.info("%d CSO tables", len(tables))

        old = {r[0] for r in conn.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'dgi_cso'")}
        view_names, infos, todo = set(), [], []
        for (mid, matrix, label, updated, sha, dim_labels, geo_dim, time_dim, stat_dim,
             n_values, dims, units, dataset_id, geo_level) in tables:
            view = ident(matrix, view_names)
            used, cols, columns = set(), [], []
            for i, dlabel in enumerate(dim_labels):
                name = ident(dlabel, used)
                cols.append(name)
                cats = list((dims[i].get("categories") or {}).values()) if i < len(dims) else []
                role = ("statistic" if i == stat_dim else "time" if i == time_dim
                        else "place" if i == geo_dim else None)
                columns.append({"name": name, "label": dlabel, "role": role,
                                "n": len(cats), "sample": cats[:SAMPLE]})
            stats = []
            if stat_dim is not None and stat_dim < len(dims):
                for code, slabel in (dims[stat_dim].get("categories") or {}).items():
                    unit = (units or {}).get(code, {}) if isinstance(units, dict) else {}
                    stats.append({"label": slabel, "unit": unit.get("label")})
            times = []
            if time_dim is not None and time_dim < len(dims):
                times = sorted((dims[time_dim].get("categories") or {}).values())
            doc = " ".join(filter(None, [
                label, matrix, " ".join(dim_labels),
                " ".join(s["label"] or "" for s in stats),
                " ".join(" ".join(c["sample"]) for c in columns if c["role"] is None)]))
            infos.append((mid, matrix, view, label, updated, sha, dataset_id,
                          json.dumps(columns), json.dumps(stats),
                          times[0] if times else None, times[-1] if times else None,
                          geo_level if geo_dim is not None else None, n_values, doc))
            todo.append((view, mid, cols))

        # Views: drop the ones no longer wanted, then replace the rest in batches.
        stale = sorted(old - view_names)
        for i in range(0, len(stale), BATCH):
            for v in stale[i:i + BATCH]:
                conn.execute(sql.SQL("DROP VIEW IF EXISTS dgi_cso.{}").format(sql.Identifier(v)))
            conn.commit()
        for i in range(0, len(todo), BATCH):
            for view, mid, cols in todo[i:i + BATCH]:
                conn.execute(sql.SQL("DROP VIEW IF EXISTS dgi_cso.{}").format(sql.Identifier(view)))
                conn.execute(view_sql(view, mid, cols))
            conn.commit()
            if (i // BATCH) % 10 == 0:
                log.info("views: %d/%d", min(i + BATCH, len(todo)), len(todo))

        conn.execute("TRUNCATE dgi_ask.cso_info")
        with conn.cursor().copy(
                "COPY dgi_ask.cso_info (matrix_id, matrix, view_name, label, updated, sha256, "
                "dataset_id, columns, statistics, time_from, time_to, geo_level, n_values, "
                "search_doc) FROM STDIN") as cp:
            for row in infos:
                cp.write_row(row)
        conn.execute("ANALYZE dgi_ask.cso_info")
        conn.commit()
        log.info("dgi_cso: %d views; dgi_ask.cso_info: %d rows; %d stale views dropped",
                 len(todo), len(infos), len(stale))

"""The tools the model uses to answer a question. Each takes plain JSON
arguments and returns a JSON-able dict, so any model provider can call them.

    search_tables   find CSO tables by meaning and by words
    describe_table  what one table holds: columns, categories, units, source
    find_place      which kind of area a place name is, and its official names
    run_sql         one read-only SELECT, with a time limit and a row limit

Plan: ~/org/20261111_dgi-search-plan.org, Level 1.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import psycopg

from .. import db, textio

log = logging.getLogger("dgi")

SEARCH_URL = "http://127.0.0.1:8002/dgi/search.json"   # the dgi-search service
SQL_TIMEOUT_MS = 20_000
SQL_MAX_ROWS = 200
LIST_CATEGORIES = 60      # categories listed in full per column by describe_table


# What the model is told about each tool (provider-neutral JSON Schema).
SPECS = [
    {
        "name": "search_tables",
        "description": (
            "Find Central Statistics Office tables that could answer a question. Searches by "
            "meaning and by words. Returns for each table its name (for describe_table and "
            "run_sql), title, columns, the period covered and the kind of area it is broken "
            "down by (county, la = local authority, ed = electoral division, sa = small area, "
            "nuts3 = region, or null for national only). Many tables exist in several versions "
            "that differ only by area level or census year; pick the one that fits. Search "
            "again with other words if nothing fits."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the table should be about, in plain words."},
                "area_level": {"type": "string",
                               "description": "Optional: only tables broken down by this kind of area.",
                               "enum": ["state", "province", "nuts2", "nuts3", "county", "la", "lea", "ed", "sa"]},
                "limit": {"type": "integer", "description": "How many tables to return (default 10)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_table",
        "description": (
            "Describe one table before querying it: every column, its categories (the exact "
            "text values to use in WHERE), the statistics and their units, when the CSO last "
            "updated it, notes, the source dataset, and three sample rows. For a column with "
            "many categories only some are listed; pass `find` to look for a category by part "
            "of its name (accents and case do not matter)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name as returned by search_tables, e.g. fy003a."},
                "find": {"type": "string", "description": "Optional: part of a category name to look for, e.g. leitrim."},
            },
            "required": ["table"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_place",
        "description": (
            "Look up an Irish place name (English or Irish, any spelling of accents) in the "
            "reference list of areas: counties, local authorities, electoral areas and "
            "divisions, regions. Returns what kind of area it is and its official name. Use "
            "it when a place could be more than one thing (Cork city or county, Galway, "
            "Dublin and its four councils)."),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Run one read-only PostgreSQL SELECT and get the rows back. Each CSO table is a "
            "view named dgi_cso.<table>, with one text column per dimension (see "
            f"describe_table), `value` (a number, NULL where not published) and `unit`. At most "
            f"{SQL_MAX_ROWS} rows come back and a query may run for {SQL_TIMEOUT_MS // 1000} "
            "seconds, so filter and aggregate in SQL. Always filter on every dimension, or "
            "group by it: most tables hold totals as well as their parts (e.g. 'Both sexes', "
            "'All ages', 'State'), and summing over them double counts."),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
]


RO_OPTIONS = (f"-c default_transaction_read_only=on -c statement_timeout={SQL_TIMEOUT_MS} "
              "-c search_path=dgi_cso,public")


def _ro_conn():
    """For our own tools: the usual login, unable to write, giving up on slow queries."""
    return psycopg.connect(db.DSN, autocommit=True, options=RO_OPTIONS)


def _ask_dsn():
    """The restricted login dgi_ask, if it has been set up (see the plan, Phase 0).

    DGI_ASK_DSN wins; otherwise a dgi_ask line in ~/.pgpass is enough.
    """
    if os.environ.get("DGI_ASK_DSN"):
        return os.environ["DGI_ASK_DSN"]
    pgpass = Path.home() / ".pgpass"
    if pgpass.exists() and ":dgi_ask:" in pgpass.read_text():
        return "host=localhost dbname=gis user=dgi_ask"
    return None


_warned = False


def _sql_conn():
    """For SQL written by the model: the dgi_ask login, which can read only dgi_cso."""
    global _warned
    dsn = _ask_dsn()
    if dsn:
        return psycopg.connect(dsn, autocommit=True, options=RO_OPTIONS)
    if not _warned:
        log.warning("role dgi_ask is not set up: model SQL runs as the normal login, read-only")
        _warned = True
    return _ro_conn()


def _vector_ranks(query):
    """Dataset ids in order of closeness of meaning, from the dgi-search service."""
    try:
        url = SEARCH_URL + "?" + urllib.parse.urlencode({"q": query, "limit": 200})
        with urllib.request.urlopen(url, timeout=60) as r:
            return [x["id"] for x in json.load(r)]
    except Exception as e:      # the service is a help, not a requirement
        log.warning("dgi-search not used: %s", e)
        return []


def search_tables(query, area_level=None, limit=10):
    query = textio.clean(query).strip()
    limit = max(1, min(int(limit or 10), 25))
    score = defaultdict(float)
    with _ro_conn() as conn:
        # Meaning: the dataset cards already embedded, mapped to their CSO table.
        ids = _vector_ranks(query)
        if ids:
            for rank, (mid,) in enumerate(conn.execute("""
                    SELECT
                           i.matrix_id
                      FROM unnest(%s::text[]) WITH ORDINALITY d(id, n)
                           JOIN dgi_ask.cso_info i ON i.dataset_id = d.id
                  ORDER BY d.n""", (ids,))):
                score[mid] += 1 / (60 + rank)
        # Words: tables holding all the words first, then any of them at half weight.
        for weight, joiner in ((1.0, "&"), (0.5, "|")):
            for rank, (mid,) in enumerate(conn.execute("""
                      WITH q AS (
                        SELECT
                               replace(plainto_tsquery('english', dgi_cat.f_key(%s))::text, '&', %s)::tsquery AS tq
                      )
                    SELECT
                           i.matrix_id
                      FROM dgi_ask.cso_info i
                         , q
                     WHERE i.tsv @@ q.tq
                  ORDER BY ts_rank_cd(to_tsvector('english', coalesce(i.label, '')), q.tq) DESC
                         , ts_rank_cd(i.tsv, q.tq, 1) DESC
                     LIMIT 100""", (query, joiner))):
                score[mid] += weight / (60 + rank)
        if not score:
            return {"tables": [], "note": "nothing found; try other words"}
        rows = conn.execute("""
            SELECT
                   matrix_id
                 , view_name
                 , label
                 , columns
                 , statistics
                 , time_from
                 , time_to
                 , geo_level
                 , n_values
                 , left(updated, 10)
              FROM dgi_ask.cso_info
             WHERE matrix_id = ANY(%s)
               AND (%s::text IS NULL OR geo_level = %s)""",
            (list(score), area_level, area_level)).fetchall()
    rows.sort(key=lambda r: -score[r[0]])
    out = []
    for _, view, label, columns, stats, t0, t1, geo, n, updated in rows[:limit]:
        out.append({
            "table": view, "title": label,
            "columns": [f'{c["name"]} ({c["n"]})' for c in columns],
            "statistics": [s["label"] for s in stats][:6],
            "period": t0 if t0 == t1 else f"{t0} to {t1}",
            "area_level": geo, "values": n, "updated": updated})
    return {"tables": out}


def _info(conn, table):
    name = table.strip().lower().removeprefix("dgi_cso.").strip('"')
    return conn.execute("""
        SELECT
               i.matrix_id
             , i.matrix
             , i.view_name
             , i.label
             , i.updated
             , i.columns
             , i.statistics
             , i.geo_level
             , i.n_values
             , j.dims
             , j.notes
             , d.title
             , d.uri
             , p.name
          FROM dgi_ask.cso_info i
               JOIN dgi_prof.jsonstat j USING (sha256)
               LEFT JOIN dgi_cat.dataset d ON d.id = i.dataset_id
               LEFT JOIN dgi_cat.publisher p ON p.id = d.publisher_id
         WHERE i.view_name = %s OR lower(i.matrix) = %s""", (name, name)).fetchone()


def describe_table(table, find=None):
    with _ro_conn() as conn:
        row = _info(conn, table)
        if not row:
            return {"error": f"no table named {table!r}; use a name from search_tables"}
        (mid, matrix, view, label, updated, columns, stats, geo, n, dims, notes,
         ds_title, ds_uri, publisher) = row
        key = textio.search_key(find) if find else None
        cols = []
        for c, d in zip(columns, dims):
            cats = list((d.get("categories") or {}).values())
            col = {"column": c["name"], "label": c["label"], "categories_total": len(cats)}
            if c["role"]:
                col["is"] = c["role"]
            if key:
                hits = [x for x in cats if key in textio.search_key(x)]
                col["matching"] = hits[:100]
            elif len(cats) <= LIST_CATEGORIES:
                col["categories"] = cats
            else:
                col["first_categories"] = cats[:30]
                col["note"] = "more exist; call describe_table with find to look one up"
            cols.append(col)
        sample = conn.execute(f'SELECT * FROM dgi_cso."{view}" LIMIT 3')
        names = [x.name for x in sample.description]
        out = {
            "table": view, "query_as": f"dgi_cso.{view}", "title": label,
            "cso_code": matrix, "last_updated_by_cso": (updated or "")[:10],
            "area_level": geo, "values": n,
            "columns": cols,
            "statistics": stats,
            "also_columns": ["value (number)", "unit (text)"],
            "sample_rows": [dict(zip(names, r)) for r in sample.fetchall()],
            "source": {"publisher": publisher or "Central Statistics Office",
                       "dataset": ds_title, "link": ds_uri,
                       "cso_link": f"https://data.cso.ie/table/{matrix}"},
        }
        notes = " ".join(x for x in (notes or []) if x).strip()
        if notes:
            out["notes"] = notes[:1500]
        return out


def find_place(name):
    with _ro_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT
                   a.level
                 , a.name
                 , a.name_ga
                 , p.name
              FROM dgi_ref.alias l
                   JOIN dgi_ref.area a USING (level, code)
                   LEFT JOIN dgi_ref.area p ON p.level = a.parent_level
                                           AND p.code = a.parent_code
             WHERE l.akey = dgi_ref.akey(%s)
          ORDER BY 1, 2
             LIMIT 40""", (textio.clean(name),)).fetchall()
    if not rows:
        return {"matches": [], "note": "not in the reference list; try describe_table with find"}
    return {"matches": [{"kind": lv, "name": nm, "name_ga": ga, "within": parent}
                        for lv, nm, ga, parent in rows]}


_SELECT = re.compile(r"^\s*(select|with)\b", re.I)


def run_sql(sql):
    q = sql.strip().rstrip(";").strip()
    if ";" in q:
        return {"error": "one statement only, without semicolons"}
    if not _SELECT.match(q):
        return {"error": "only SELECT (or WITH ... SELECT) is allowed"}
    try:
        with _sql_conn() as conn:
            cur = conn.execute(q)
            names = [c.name for c in cur.description]
            rows = cur.fetchmany(SQL_MAX_ROWS + 1)
    except psycopg.Error as e:
        msg = (e.diag.message_primary or str(e)) if hasattr(e, "diag") else str(e)
        hint = getattr(e.diag, "message_hint", None) if hasattr(e, "diag") else None
        return {"error": msg + (f" Hint: {hint}" if hint else "")}
    out = {"columns": names,
           "rows": [[v if isinstance(v, (int, float, str, type(None))) else str(v) for v in r]
                    for r in rows[:SQL_MAX_ROWS]]}
    if len(rows) > SQL_MAX_ROWS:
        out["truncated"] = f"only the first {SQL_MAX_ROWS} rows are shown; narrow or aggregate the query"
    return out


_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


def sources_for(sqls):
    """The CSO tables named in these queries, for the list of sources under an answer."""
    names = sorted({w for q in sqls for w in _IDENT.findall(q.lower())})
    if not names:
        return []
    with _ro_conn() as conn:
        rows = conn.execute("""
            SELECT
                   i.view_name
                 , i.matrix
                 , i.label
                 , left(i.updated, 10)
                 , i.time_from
                 , i.time_to
                 , d.uri
              FROM dgi_ask.cso_info i
                   LEFT JOIN dgi_cat.dataset d ON d.id = i.dataset_id
             WHERE i.view_name = ANY(%s)
          ORDER BY i.matrix""", (names,)).fetchall()
    return [{"table": v, "code": m, "title": label, "updated": upd,
             "period": t0 if t0 == t1 else f"{t0} to {t1}",
             "publisher": "Central Statistics Office",
             "link": f"https://data.cso.ie/table/{m}", "catalogue_link": uri}
            for v, m, label, upd, t0, t1, uri in rows]


FUNCS = {"search_tables": search_tables, "describe_table": describe_table,
         "find_place": find_place, "run_sql": run_sql}


def call(name, args):
    """Run a tool by name. Never raises: an error comes back as {"error": ...}."""
    fn = FUNCS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}

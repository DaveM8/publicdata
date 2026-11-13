"""Phase 4c: load map layers into PostGIS (schema dgi_geo).

Each vector layer found by the profile becomes one table, in ITM (EPSG:2157),
named geo_<first 8 of sha256>_<n>. Then:

- attribute text that GDAL read double-encoded is repaired with textio and
  every change is logged in dgi_prof.text_fix;
- the layer is placed exactly: features are counted per council
  (dgi_link.layer_place_exact), replacing the bounding-box guess;
- the martin role may read the table, so the vector tile server can serve it
  (after a martin restart).

Shapefiles get the SHAPE_ENCODING the profile decided. Layers without a known
coordinate system get the one guessed from their extent. Layers over
MAX_FEATURES are skipped and recorded; load them by hand if needed.
"""

import hashlib
import logging
import re
import subprocess
import tempfile
from multiprocessing import Pool

from psycopg import sql

from . import db, profile, textio

log = logging.getLogger("dgi")

MAX_FEATURES = 2_000_000
TIMEOUT = 3600

SETUP = """
CREATE TABLE IF NOT EXISTS dgi_geo.layer_table (
    table_name     text PRIMARY KEY,
    sha256         text NOT NULL,
    part           text NOT NULL,
    layer          text NOT NULL,
    features       bigint,
    geom_type      text,
    srid_from      int,
    shape_encoding text,
    text_fixes     int,
    error          text,
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sha256, part, layer)
);
CREATE TABLE IF NOT EXISTS dgi_link.layer_place_exact (
    table_name  text NOT NULL,
    la_code     text NOT NULL,
    features    bigint,
    share       real,              -- fraction of the layer's features in this council
    PRIMARY KEY (table_name, la_code)
);
"""


def _todo(conn, redo=False, limit=None):
    q = """
        SELECT
               l.sha256
             , l.part
             , l.layer
             , l.gdal_path
             , l.feature_count
             , coalesce(l.srid, dgi_link.guess_srid(l.extent)) AS srid
             , l.srid IS NULL AS srid_guessed
             , l.encoding
          FROM dgi_prof.layer l
         WHERE l.geom_type IS NOT NULL                      -- not attribute-only tables
           AND l.geom_type NOT IN ('raster', 'None')         -- (e.g. geodatabase bookkeeping)
           AND coalesce(l.feature_count, 0) > 0
    """
    if not redo:
        q += " AND NOT EXISTS (SELECT FROM dgi_geo.layer_table t WHERE t.sha256 = l.sha256 AND t.part = l.part AND t.layer = l.layer)"
    q += " ORDER BY l.feature_count"
    rows = conn.execute(q).fetchall()
    return rows[:limit] if limit else rows


def _table_name(conn, sha256, part, layer):
    """geo_<first 8 of the file's sha256>_<6 from a hash of member and layer name>.
    Derived from the layer itself, so parallel workers never pick the same name."""
    row = conn.execute("SELECT table_name FROM dgi_geo.layer_table WHERE sha256 = %s AND part = %s AND layer = %s",
                       (sha256, part, layer)).fetchone()
    if row:
        return row[0]
    return f"geo_{sha256[:8]}_{hashlib.md5(f'{part}|{layer}'.encode()).hexdigest()[:6]}"


def _fix_text(conn, table, path):
    """Repair double-encoded attribute text in a loaded table; returns the number of values changed."""
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'dgi_geo' AND table_name = %s AND data_type IN ('text', 'character varying')",
        (table,))]
    fixed = 0
    for col in cols:
        rows = conn.execute(sql.SQL("SELECT fid, {c} FROM dgi_geo.{t} WHERE {c} ~ %s").format(
            c=sql.Identifier(col), t=sql.Identifier(table)), (textio.MOJIBAKE.pattern,)).fetchall()
        for fid, val in rows:
            new = textio.clean(val)
            if new != val:
                conn.execute(sql.SQL("UPDATE dgi_geo.{t} SET {c} = %s WHERE fid = %s").format(
                    c=sql.Identifier(col), t=sql.Identifier(table)), (new, fid))
                if fixed < 500:
                    conn.execute("INSERT INTO dgi_prof.text_fix (source, ref, field, before, after) "
                                 "VALUES ('geo', %s, %s, %s, %s)", (path, f"{table}.{col}", val, new))
                fixed += 1
    return fixed


def _source(conn, sha256, part, gdal_path, tmpdir):
    """A GDAL path that exists now. The profile opened some layers from
    temporary copies: members pulled out of zips, and MapInfo/shapefile
    layers put back together from separately published pieces."""
    if not gdal_path.startswith(str(profile.TMP_ROOT)):
        return gdal_path
    rel = conn.execute("SELECT path FROM dgi_prof.file WHERE sha256 = %s", (sha256,)).fetchone()[0]
    path = db.MIRROR / rel
    if part:
        return f"/vsizip/{path}/{part}"
    target, _ = profile.assemble_layer(path, tmpdir)
    return str(target)


def load_one(task):
    sha256, part, layer, gdal_path, features, srid, srid_guessed, encoding = task
    profile.TMP_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=profile.TMP_ROOT) as tmpdir, db.connect(autocommit=True) as conn:
        gdal_path = _source(conn, sha256, part, gdal_path, tmpdir)
        return _load(conn, sha256, part, layer, gdal_path, features, srid, srid_guessed, encoding)


def _load(conn, sha256, part, layer, gdal_path, features, srid, srid_guessed, encoding):
    if True:
        table = _table_name(conn, sha256, part, layer)
        if features and features > MAX_FEATURES:
            conn.execute("""INSERT INTO dgi_geo.layer_table (table_name, sha256, part, layer, features, error)
                            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (table_name) DO UPDATE SET error = EXCLUDED.error""",
                         (table, sha256, part, layer, features, f"skipped: over {MAX_FEATURES:,} features"))
            return table, "skipped"
        shape_enc = encoding.split(" ", 1)[0] if encoding else None
        cmd = ["ogr2ogr", "-f", "PostgreSQL", "PG:dbname=gis", gdal_path, layer,
               "-nln", f"dgi_geo.{table}", "-overwrite",
               "-nlt", "PROMOTE_TO_MULTI", "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid",
               "-lco", "PRECISION=NO", "-lco", "SPATIAL_INDEX=GIST", "-makevalid", "-forceNullable",
               # dates load as text: shapefiles hold values like 0000/00/00
               "-fieldTypeToString", "Date,Time,DateTime",
               "--config", "PG_USE_COPY", "YES"]
        # Into ITM. A layer with no coordinate system that doesn't look Irish
        # either is loaded as it is, and not placed on the map.
        if srid:
            cmd += ["-t_srs", "EPSG:2157"]
            if srid_guessed:
                cmd += ["-s_srs", f"EPSG:{srid}"]
        if shape_enc:
            cmd += ["--config", "SHAPE_ENCODING", shape_enc]
        p = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
        err = None
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip()[-800:]
            # One retry, adjusted for what went wrong.
            retry = list(cmd)
            if "UTF-8" in err or "invalid byte sequence" in err:
                # The layer's text is Windows-1252 although the format promises UTF-8
                # (0xB2 is "²", as in m²). Postgres converts it; any double encoding
                # this creates is repaired by _fix_text below.
                retry += ["--config", "PGCLIENTENCODING", "WIN1252"]
            if "Geometry to be inserted is of type" in err:
                retry[retry.index("PROMOTE_TO_MULTI")] = "GEOMETRY"
            if "json" in err.lower() or "invalid input syntax" in err:
                retry[retry.index("Date,Time,DateTime")] = "All"
            if retry != cmd:
                p = subprocess.run(retry, capture_output=True, timeout=TIMEOUT)
                err = None if p.returncode == 0 else p.stderr.decode("utf-8", "replace").strip()[-800:]
        fixes = 0
        n = gtype = None
        if not err:
            try:
                # GeoPackages and geodatabases keep their own geometry column name
                # (often "Shape"); make every table use geom.
                gcol = conn.execute("SELECT f_geometry_column FROM geometry_columns "
                                    "WHERE f_table_schema = 'dgi_geo' AND f_table_name = %s LIMIT 1",
                                    (table,)).fetchone()
                if gcol and gcol[0] != "geom":
                    conn.execute(sql.SQL("ALTER TABLE dgi_geo.{t} RENAME COLUMN {c} TO geom").format(
                        t=sql.Identifier(table), c=sql.Identifier(gcol[0])))
                # Some layers mark missing positions with ±DBL_MAX (-1.8e308). Those
                # can't be reprojected, so martin fails on the table's bounds and exits.
                conn.execute(sql.SQL("""
                    UPDATE dgi_geo.{t}
                       SET geom = NULL
                     WHERE greatest(abs(ST_XMin(geom)), abs(ST_XMax(geom)),
                                    abs(ST_YMin(geom)), abs(ST_YMax(geom))) > 1e10
                """).format(t=sql.Identifier(table)))
                path = conn.execute("SELECT path FROM dgi_prof.file WHERE sha256 = %s", (sha256,)).fetchone()[0]
                fixes = _fix_text(conn, table, path)
                n, gtype = conn.execute(sql.SQL(
                    "SELECT count(*), max(GeometryType(geom)) FROM dgi_geo.{t}").format(t=sql.Identifier(table))).fetchone()
                conn.execute("DELETE FROM dgi_link.layer_place_exact WHERE table_name = %s", (table,))
                if srid:
                    conn.execute(sql.SQL("""
                    INSERT INTO dgi_link.layer_place_exact (table_name, la_code, features, share)
                      SELECT
                             %s
                           , s.code
                           , count(DISTINCT g.fid)
                           , count(DISTINCT g.fid)::real / nullif(%s, 0)
                        FROM dgi_geo.{t} g
                             JOIN dgi_ref.la_sub s ON ST_Intersects(s.geom, ST_PointOnSurface(g.geom))
                    GROUP BY s.code
                """).format(t=sql.Identifier(table)), (table, n))
                conn.execute(sql.SQL("GRANT SELECT ON dgi_geo.{t} TO martin").format(t=sql.Identifier(table)))
            except Exception as e:
                err = f"after load: {type(e).__name__}: {e}"[:800]
        conn.execute("""
            INSERT INTO dgi_geo.layer_table (table_name, sha256, part, layer, features, geom_type, srid_from,
                                             shape_encoding, text_fixes, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (table_name) DO UPDATE
               SET features = EXCLUDED.features, geom_type = EXCLUDED.geom_type, srid_from = EXCLUDED.srid_from,
                   shape_encoding = EXCLUDED.shape_encoding, text_fixes = EXCLUDED.text_fixes,
                   error = EXCLUDED.error, loaded_at = now()""",
            (table, sha256, part, layer, n, gtype, srid, shape_enc, fixes, err))
        return table, err


def run(dry_run=False, workers=6, limit=None, redo=False, failed=False):
    with db.connect() as conn:
        conn.execute(SETUP)
        if failed and not dry_run:
            # Forget failed layers so they're tried again.
            for (t,) in conn.execute("SELECT table_name FROM dgi_geo.layer_table WHERE error IS NOT NULL "
                                     "AND error NOT LIKE 'skipped%%'").fetchall():
                conn.execute(sql.SQL("DROP TABLE IF EXISTS dgi_geo.{t}").format(t=sql.Identifier(t)))
            conn.execute("DELETE FROM dgi_geo.layer_table WHERE error IS NOT NULL AND error NOT LIKE 'skipped%%'")
        conn.execute("GRANT USAGE ON SCHEMA dgi_geo TO martin")
        conn.commit()
        todo = _todo(conn, redo=redo, limit=limit)
    log.info("%d layers to load", len(todo))
    if dry_run:
        return
    done = errors = 0
    with Pool(workers) as pool:
        for table, err in pool.imap_unordered(load_one, todo):
            done += 1
            if err and err != "skipped":
                errors += 1
                log.warning("%s: %s", table, re.sub(r"\s+", " ", err)[:300])
            if done % 200 == 0:
                log.info("loaded %d/%d layers, %d errors", done, len(todo), errors)
    log.info("loaded %d layers, %d errors", done, errors)

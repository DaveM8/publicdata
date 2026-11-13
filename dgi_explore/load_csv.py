"""Phase 4b: load tidy CSV/TSV files as tables in dgi_data.

One table per file: csv_<first 8 of sha256> for a plain CSV, and
csv_<first 8 of sha256>_<6 from the member name> for a CSV inside a zip
(zips with up to MAX_MEMBERS CSVs; a few zips hold hundreds of thousands of
tiny files, which stay as files). Column names come from the header row the
profile found (title lines above it are skipped); types come from the
profile: int -> bigint, float -> double precision, year -> int, everything
else text. If a typed load fails, the file is loaded again as all text.

Text goes through textio like everything else, so Irish characters are right
and double encoding is repaired. Registry: dgi_data.csv_table. Afterwards,
dataset_views() makes one view per dataset joining its tables that share the
same columns: dgi_data.ds_<first 8 of the dataset id>.
"""

import csv
import hashlib
import logging
import re
import shutil
import tempfile
import zipfile
from multiprocessing import Pool
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from . import db, profile, textio

log = logging.getLogger("dgi")

PG_TYPE = {"int": "bigint", "float": "double precision", "year": "int"}
MAX_MEMBERS = 50                 # zips with more CSVs than this stay as files
MAX_MEMBER_BYTES = 8 << 30

SETUP = """
CREATE TABLE IF NOT EXISTS dgi_data.csv_table (
    table_name  text PRIMARY KEY,
    sha256      text NOT NULL,
    path        text,
    columns     jsonb,          -- [[column name, original header, type], ...]
    rows        bigint,
    typed       boolean,        -- false: loaded as text after a typed load failed
    error       text,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);
-- Added 15 Oct: CSVs inside zips. member is the file's path inside the zip ('' for a plain CSV).
ALTER TABLE dgi_data.csv_table ADD COLUMN IF NOT EXISTS member text NOT NULL DEFAULT '';
ALTER TABLE dgi_data.csv_table DROP CONSTRAINT IF EXISTS csv_table_sha256_key;
CREATE UNIQUE INDEX IF NOT EXISTS csv_table_file ON dgi_data.csv_table (sha256, member);
"""


def column_names(header):
    """Postgres column names from a header: lower case, underscores, unique, <= 60 chars."""
    names, seen = [], set()
    for i, h in enumerate(header):
        n = textio.search_key(h)
        n = re.sub(r"[^a-z0-9]+", "_", n).strip("_")[:60] or f"column_{i + 1}"
        if n[0].isdigit():
            n = "c_" + n
        base, k = n, 2
        while n in seen:
            n = f"{base[:56]}_{k}"
            k += 1
        seen.add(n)
        names.append(n)
    return names


def table_name(sha256, member=""):
    if not member:
        return f"csv_{sha256[:8]}"
    return f"csv_{sha256[:8]}_{hashlib.md5(member.encode()).hexdigest()[:6]}"


def _convert(v, t):
    v = v.strip()
    if not v:
        return None
    if t in ("int", "year"):
        return int(v.replace(",", ""))
    if t == "float":
        return float(v.replace(",", ""))
    return v


def _sniff_delimiter(full, enc):
    sample = "".join(line for _, line in zip(range(200), textio.iter_lines(full, enc)))
    try:
        return csv.Sniffer().sniff(sample[:65536], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _load_file(full, table, sha256, path, member, delim, types):
    enc = textio.detect_encoding(full)
    delim = delim or _sniff_delimiter(full, enc)
    head = []
    for r in csv.reader(textio.iter_lines(full, enc), delimiter=delim):
        head.append(r)
        if len(head) >= 15:
            break
    hi, width = profile.find_header(head)
    if hi is None:
        raise ValueError("no header found")
    header = [h.strip() for h in head[hi]]
    names = column_names(header)
    col_types = [types.get(i, "text") for i in range(width)]
    for typed in (True, False):
        pg_types = [PG_TYPE.get(t, "text") if typed else "text" for t in col_types]
        try:
            with db.connect() as conn:
                conn.execute(sql.SQL("DROP TABLE IF EXISTS dgi_data.{t}").format(t=sql.Identifier(table)))
                conn.execute(sql.SQL("CREATE TABLE dgi_data.{t} ({cols})").format(
                    t=sql.Identifier(table),
                    cols=sql.SQL(", ").join(sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(pt))
                                            for n, pt in zip(names, pg_types))))
                n = 0
                with conn.cursor().copy(sql.SQL("COPY dgi_data.{t} FROM STDIN").format(
                        t=sql.Identifier(table))) as cp:
                    for i, r in enumerate(csv.reader(textio.iter_lines(full, enc), delimiter=delim)):
                        if i <= hi or not any(c.strip() for c in r):
                            continue
                        r = (r + [""] * width)[:width]
                        cp.write_row([_convert(v, t) if typed else (v.strip() or None)
                                      for v, t in zip(r, col_types)])
                        n += 1
                conn.execute("""
                    INSERT INTO dgi_data.csv_table (table_name, sha256, member, path, columns, rows, typed, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT (table_name) DO UPDATE
                       SET columns = EXCLUDED.columns, rows = EXCLUDED.rows, typed = EXCLUDED.typed,
                           error = NULL, loaded_at = now()""",
                    (table, sha256, member, path,
                     Jsonb([[nm, h, pt] for nm, h, pt in zip(names, header, pg_types)]), n, typed))
            return n
        except Exception as e:
            # A value the profile's sample didn't see (text in a number column,
            # a number too big for bigint): load it all as text.
            if not typed:
                raise
            log.debug("%s: typed load failed (%s), loading as text", table, e)


def load_one(task):
    sha256, path, member, delim, types = task
    table = table_name(sha256, member)
    try:
        if not member:
            return table, _load_file(db.MIRROR / path, table, sha256, path, "", delim, types), None
        profile.TMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=profile.TMP_ROOT) as tmp:
            local = Path(tmp) / ("m" + Path(member).suffix.lower())
            with zipfile.ZipFile(db.MIRROR / path) as z, z.open(member) as src, open(local, "wb") as dst:
                shutil.copyfileobj(src, dst, 16 << 20)
            return table, _load_file(local, table, sha256, path, member, None, types), None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:500]
        with db.connect() as conn:
            conn.execute("""
                INSERT INTO dgi_data.csv_table (table_name, sha256, member, path, error) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (table_name) DO UPDATE SET error = EXCLUDED.error, loaded_at = now()""",
                (table, sha256, member, path, err))
        return table, 0, err


def _zip_members(conn, redo):
    """(sha256, path, member, None, types) for CSVs inside zips with at most MAX_MEMBERS of them."""
    done = set() if redo else {(s, m) for s, m in conn.execute(
        "SELECT sha256, member FROM dgi_data.csv_table WHERE member <> ''")}
    types = {}
    for sha, part, ordinal, t in conn.execute("""
            SELECT c.sha256, c.part, c.ordinal, c.inferred_type
              FROM dgi_prof.col c JOIN dgi_prof.file f USING (sha256)
             WHERE f.real_type = 'zip' AND c.part <> ''"""):
        types.setdefault((sha, part), {})[ordinal] = t
    out = []
    for sha, path in conn.execute("SELECT sha256, path FROM dgi_prof.file WHERE real_type = 'zip' AND error IS NULL"):
        try:
            with zipfile.ZipFile(db.MIRROR / path) as z:
                infos = z.infolist()
        except Exception:
            continue
        names = [i.filename for i in infos]
        if any(n.startswith("xl/") for n in names) or "word/document.xml" in names:
            continue
        members = [i for i in infos if i.filename.lower().endswith((".csv", ".tsv")) and "__MACOSX" not in i.filename
                   and 0 < i.file_size <= MAX_MEMBER_BYTES]
        if not members or len(members) > MAX_MEMBERS:
            continue
        for i in members:
            if (sha, i.filename) not in done:
                out.append((sha, path, i.filename, None, types.get((sha, i.filename), {})))
    return out


def run(dry_run=False, workers=10, limit=None, redo=False, zips=True):
    with db.connect() as conn:
        conn.execute(SETUP)
        conn.commit()
        q = """
            SELECT
                   f.sha256
                 , f.path
                 , ''
                 , f.summary->>'delimiter'
                 , coalesce(jsonb_object_agg(c.ordinal, c.inferred_type)
                            FILTER (WHERE c.ordinal IS NOT NULL), '{}')
              FROM dgi_prof.file f
                   LEFT JOIN dgi_prof.col c ON c.sha256 = f.sha256 AND c.part = ''
             WHERE f.kind = 'table'
               AND f.real_type IN ('text', 'utf16-text')
               AND f.summary ? 'delimiter'
               AND f.error IS NULL
               AND coalesce(f.n_cols, 0) >= 2
               AND coalesce(f.n_rows, 0) >= 1
        """
        if not redo:
            q += " AND NOT EXISTS (SELECT FROM dgi_data.csv_table t WHERE t.sha256 = f.sha256 AND t.member = '')"
        q += " GROUP BY f.sha256, f.path, f.summary ORDER BY f.bytes"
        todo = [(s, p, m, d, {int(k): v for k, v in t.items()}) for s, p, m, d, t in conn.execute(q).fetchall()]
        if zips:
            members = _zip_members(conn, redo)
            log.info("%d CSVs inside zips to load", len(members))
            todo += members
    if limit:
        todo = todo[:limit]
    log.info("%d CSV files to load", len(todo))
    if dry_run:
        return
    done = errors = rows = 0
    with Pool(workers) as pool:
        for table, n, err in pool.imap_unordered(load_one, todo, chunksize=1):
            done += 1
            rows += n
            if err:
                errors += 1
                log.warning("%s: %s", table, err[:200])
            if done % 500 == 0:
                log.info("loaded %d/%d files, %d rows, %d errors", done, len(todo), rows, errors)
    log.info("loaded %d files, %d rows, %d errors", done, rows, errors)
    dataset_views()


# ── one view per dataset ─────────────────────────────────────────────────────

def view_name(dataset_id, n=1):
    return f"ds_{dataset_id[:8]}" + (f"_{n}" if n > 1 else "")


def dataset_views():
    """dgi_data.ds_<dataset id 8>: the dataset's CSV tables that share the same
    columns, stacked, with a source_file column. A dataset whose files have
    different columns gets ds_<id8>, ds_<id8>_2, ... one per layout."""
    # Autocommit: thousands of views in one transaction run out of lock slots.
    with db.connect(autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dgi_data.dataset_view (
                view_name   text PRIMARY KEY,
                dataset_id  text NOT NULL,
                tables      text[] NOT NULL,
                files       text[] NOT NULL,
                rows        bigint
            )""")
        for (v,) in conn.execute("SELECT view_name FROM dgi_data.dataset_view").fetchall():
            conn.execute(sql.SQL("DROP VIEW IF EXISTS dgi_data.{v}").format(v=sql.Identifier(v)))
        conn.execute("TRUNCATE dgi_data.dataset_view")
        rows = conn.execute("""
            SELECT DISTINCT ON (r.dataset_id, c.table_name)
                   r.dataset_id
                 , c.table_name
                 , c.columns
                 , coalesce(nullif(c.member, ''), r.title, c.path) AS file
                 , c.rows
              FROM dgi_data.csv_table c
                   JOIN dgi_cat.resource r ON r.sha256 = c.sha256
             WHERE c.error IS NULL
          ORDER BY r.dataset_id, c.table_name, r.title""").fetchall()
        groups = {}
        for did, table, cols, file, n in rows:
            sig = tuple((c[0], c[2]) for c in cols)
            groups.setdefault(did, {}).setdefault(sig, []).append((table, file, n))
        made = 0
        for did, by_sig in groups.items():
            for k, (sig, members) in enumerate(sorted(by_sig.items(), key=lambda kv: -sum(m[2] or 0 for m in kv[1])), 1):
                v = view_name(did, k)
                # The file name column: source_file, unless the data already has one.
                fcol = "dgi_source_file" if any(c[0] == "source_file" for c in sig) else "source_file"
                parts = [sql.SQL("SELECT {f}::text AS {c}, * FROM dgi_data.{t}").format(
                    f=sql.Literal(file), c=sql.Identifier(fcol), t=sql.Identifier(t))
                    for t, file, _ in sorted(members, key=lambda m: m[1])]
                with conn.transaction():
                    conn.execute(sql.SQL("CREATE VIEW dgi_data.{v} AS ").format(v=sql.Identifier(v))
                                 + sql.SQL(" UNION ALL ").join(parts))
                    conn.execute("INSERT INTO dgi_data.dataset_view VALUES (%s, %s, %s, %s, %s)",
                                 (v, did, [m[0] for m in members], [m[1] for m in members],
                                  sum(m[2] or 0 for m in members)))
                    conn.execute(sql.SQL("GRANT SELECT ON dgi_data.{v} TO dgi_reader").format(v=sql.Identifier(v)))
                made += 1
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA dgi_data TO dgi_reader")
    log.info("dataset views: %d", made)

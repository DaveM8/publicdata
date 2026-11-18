"""Load the Oireachtas export from the Yes Deputy server into dgi_oir.

The export (made there with export_vectors.py / export_new_vectors.py /
export_answers.py, see ~/org/20261112_oireachtas-data-report.org) is:

    EXPORT/vectors/*.parquet         one row per embedded chunk, with its vector
    EXPORT/answers_*.parquet         whole answers (written and oral)

Every vectors file is loaded once (dgi_oir.loaded_file); a chunk id already in
the table is skipped, so a top-up file that overlaps an older one is harmless.
Answers are upserted by id, so a newer export corrects an older one.

Indexes are built by separate options, after the first big load:
    --index         record, date and word-search indexes (minutes)
    --vector-index  the HNSW search index over 7.8 million vectors (hours)
"""

import logging
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger("dgi")

EXPORT = Path("/mnt/zddb/export")

# Column order of dgi_oir.chunk as loaded (tsv is generated).
CHUNK_COLS = [
    ("chunk_id", "text"), ("record_id", "text"), ("chunk_index", "int4"), ("chunk_type", "text"),
    ("house", "text"), ("committee", "text"), ("doc_type", "text"), ("date", "text"),
    ("year", "int2"), ("section_id", "text"), ("section_type", "text"), ("heading", "text"),
    ("heading_path", "text"), ("department_role", "text"), ("answering_person", "text"),
    ("answering_role", "text"), ("asking_persons", "text[]"), ("questions", "text[]"),
    ("question_numbers", "int4[]"), ("speaker", "text"), ("speaker_role", "text"),
    ("speakers", "text[]"), ("table_caption", "text"), ("table_headers", "text[]"),
    ("body", "text"), ("model", "text"), ("embedding", "vector"),
]
# Parquet name where it differs from ours.
PARQUET_NAME = {"body": "text"}
# Scalar text fields where the export has '' for "not applicable": stored as NULL.
BLANK_TO_NULL = {"committee", "doc_type", "section_type", "heading_path", "department_role",
                 "answering_person", "answering_role", "speaker", "speaker_role", "table_caption"}

ANSWER_COLS = ["id", "date", "year", "house", "heading", "section_id", "department_role",
               "answering_person", "answering_role", "question_numbers", "questions",
               "answer_clean", "doc_type"]


def _stage_sql():
    cols = ", ".join(f"{n} {'text' if t == 'vector' else t}" for n, t in CHUNK_COLS)
    return f"CREATE TEMP TABLE stage ({cols.replace('embedding text', 'embedding vector(1536)')})"


def _insert_sql():
    exprs = []
    for n, _ in CHUNK_COLS:
        if n == "date":
            exprs.append("date::date")
        elif n in BLANK_TO_NULL:
            exprs.append(f"nullif({n}, '')")
        else:
            exprs.append(n)
    names = ", ".join(n for n, _ in CHUNK_COLS)
    return (f"INSERT INTO dgi_oir.chunk ({names}) SELECT {', '.join(exprs)} FROM stage "
            "ON CONFLICT (chunk_id) DO NOTHING")


def _load_vectors_file(path: str) -> tuple[str, int, int]:
    """Load one vectors Parquet file, a row group at a time. Runs in a worker process."""
    import numpy as np
    import pyarrow.parquet as pq
    from pgvector.psycopg import register_vector

    pf = pq.ParquetFile(path)
    read = added = 0
    t0 = time.time()
    with db.connect() as conn:
        register_vector(conn)
        conn.execute(_stage_sql())
        insert = _insert_sql()
        types = [t for _, t in CHUNK_COLS]
        for g in range(pf.num_row_groups):
            t = pf.read_row_group(g)
            vecs = t.column("embedding").combine_chunks().flatten().to_numpy().reshape(-1, 1536)
            vecs = np.ascontiguousarray(vecs, dtype=np.float32)
            cols = [t.column(PARQUET_NAME.get(n, n)).to_pylist() for n, _ in CHUNK_COLS[:-1]]
            with conn.cursor() as cur:
                cur.execute("TRUNCATE stage")
                with cur.copy("COPY stage FROM STDIN WITH (FORMAT BINARY)") as cp:
                    cp.set_types(types)
                    for i, row in enumerate(zip(*cols)):
                        cp.write_row((*row, vecs[i]))
                cur.execute(insert)
                added += cur.rowcount
            conn.commit()
            read += t.num_rows
            if g % 20 == 0:
                log.info("%s: %d/%d row groups, %s rows, %.0f rows/s", Path(path).name, g + 1,
                         pf.num_row_groups, f"{read:,}", read / (time.time() - t0))
    return path, read, added


def _file_key(p: Path):
    st = p.stat()
    return st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc)


def load_vectors(export: Path, workers: int, dry_run: bool, limit_files: int | None):
    files = sorted((export / "vectors").glob("*.parquet"))
    with db.connect() as conn:
        done = {r[0]: (r[1], r[2]) for r in
                conn.execute("SELECT path, size, mtime FROM dgi_oir.loaded_file")}
    todo = [p for p in files if done.get(str(p)) != _file_key(p)]
    if limit_files:
        todo = todo[:limit_files]
    log.info("vectors: %d files in %s, %d to load", len(files), export / "vectors", len(todo))
    for p in todo:
        log.info("  %s (%.1f GB)", p.name, p.stat().st_size / 1e9)
    if dry_run or not todo:
        return
    with ProcessPoolExecutor(min(workers, len(todo))) as pool:
        for path, read, added in pool.map(_load_vectors_file, [str(p) for p in todo]):
            size, mtime = _file_key(Path(path))
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO dgi_oir.loaded_file (path, size, mtime, rows_read, rows_added) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (path) DO UPDATE SET size = excluded.size, "
                    "mtime = excluded.mtime, rows_read = excluded.rows_read, "
                    "rows_added = excluded.rows_added, loaded_at = now()",
                    (path, size, mtime, read, added))
            log.info("%s: done, %s rows read, %s new", Path(path).name, f"{read:,}", f"{added:,}")


def load_answers(export: Path, dry_run: bool):
    import pyarrow.parquet as pq

    files = sorted(export.glob("answers_*.parquet"))
    log.info("answers: %d files", len(files))
    if dry_run:
        return
    names = ", ".join(ANSWER_COLS + ["source_file"])
    sets = ", ".join(f"{c} = excluded.{c}" for c in ANSWER_COLS[1:] + ["source_file"])
    with db.connect() as conn:
        for p in files:
            t = pq.read_table(p, columns=ANSWER_COLS)
            rows = list(zip(*[t.column(c).to_pylist() for c in ANSWER_COLS]))
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO dgi_oir.answer ({names}) "
                    f"VALUES ({', '.join(['%s'] * (len(ANSWER_COLS) + 1))}) "
                    f"ON CONFLICT (id) DO UPDATE SET {sets}",
                    [(*r, str(p)) for r in rows])
            conn.commit()
            log.info("%s: %s answers", p.name, f"{len(rows):,}")


INDEXES = [
    "CREATE INDEX IF NOT EXISTS chunk_record ON dgi_oir.chunk (record_id, chunk_index)",
    "CREATE INDEX IF NOT EXISTS chunk_date ON dgi_oir.chunk (date)",
    "CREATE INDEX IF NOT EXISTS chunk_tsv ON dgi_oir.chunk USING gin (tsv)",
    # Filters Yes Deputy applies: house and years, and a member by exact name.
    "CREATE INDEX IF NOT EXISTS chunk_house_year ON dgi_oir.chunk (house, year)",
    "CREATE INDEX IF NOT EXISTS chunk_speaker ON dgi_oir.chunk (speaker) WHERE speaker IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS chunk_answering ON dgi_oir.chunk (answering_person) WHERE answering_person IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS chunk_asking ON dgi_oir.chunk USING gin (asking_persons)",
    "CREATE INDEX IF NOT EXISTS chunk_speakers ON dgi_oir.chunk USING gin (speakers)",
    "CREATE INDEX IF NOT EXISTS answer_date ON dgi_oir.answer (date)",
    # Ranked word search (pg_textsearch must be installed and preloaded:
    # deploy/pg-textsearch-setup.sh). Queries repeat this exact expression.
    "CREATE INDEX IF NOT EXISTS chunk_bm25 ON dgi_oir.chunk USING bm25 ("
    "(dgi_oir.chunk_bm25_text(speaker, answering_person, heading, asking_persons,"
    " heading_path, questions, body))) WITH (text_config = 'dgi_oir.english_nostem')",
]
# Half precision: the exported values carry at most 10 mantissa bits, as float16 does.
VECTOR_INDEX = ("CREATE INDEX IF NOT EXISTS chunk_embedding ON dgi_oir.chunk "
                "USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)")


def build_indexes(vector: bool, mem: str, workers: int):
    with db.connect(autocommit=True) as conn:
        conn.execute(f"SET maintenance_work_mem = '{mem}'")
        conn.execute(f"SET max_parallel_maintenance_workers = {int(workers)}")
        for sql in INDEXES + ([VECTOR_INDEX] if vector else []):
            log.info("%s", sql)
            t0 = time.time()
            conn.execute(sql)
            log.info("  %.0f s", time.time() - t0)
        conn.execute("ANALYZE dgi_oir.chunk")
        conn.execute("ANALYZE dgi_oir.answer")


def run(export: Path = EXPORT, dry_run=False, workers=5, limit_files=None,
        index=False, vector_index=False, mem="32GB", index_workers=7):
    with db.connect() as conn:
        db.run_sql_files(conn, "55_oir")
    if index or vector_index:
        build_indexes(vector_index, mem, index_workers)
        return
    load_answers(export, dry_run)
    load_vectors(export, workers, dry_run, limit_files)
    if not dry_run:
        with db.connect() as conn:
            n = conn.execute("SELECT count(*) FROM dgi_oir.chunk").fetchone()[0]
            a = conn.execute("SELECT count(*) FROM dgi_oir.answer").fetchone()[0]
        log.info("dgi_oir.chunk: %s rows; dgi_oir.answer: %s rows", f"{n:,}", f"{a:,}")

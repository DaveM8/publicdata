"""The web app's own records in Postgres (schema app): one row per question."""

import hashlib
import json
import secrets
from datetime import date
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import db, textio

SECRET_FILE = Path.home() / ".config/dgi-explore/web-secret"
STALE_AFTER = "10 minutes"     # a 'working' row older than this was interrupted


def setup():
    with db.connect() as conn:
        db.run_sql_files(conn, "70_app")


def _secret():
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_hex(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_text().strip()


def client_hash(address):
    """Who is asking, for the daily limit only. Changes every day, so it cannot
    be used to follow a person over time, and the address itself is never stored."""
    return hashlib.sha256(f"{_secret()}|{date.today()}|{address}".encode()).hexdigest()[:24]


_owners = None


def owner(code):
    """The body a CSO-hosted table belongs to when it isn't the CSO itself
    (Transport Infrastructure Ireland for NRA01, ...), else None. Read once from
    each table's JSON-stat copyright, and used when a page is shown, so answers
    saved before 20 Nov are right too."""
    global _owners
    if _owners is None:
        try:
            _owners = _read_owners()
        except Exception:        # no table index here: say CSO, as before, rather than fail
            return None
    return _owners.get((code or "").upper())


def _read_owners():
    with db.connect() as conn:
        return dict(conn.execute("""
            SELECT
                   upper(i.matrix)
                 , j.extension -> 'copyright' ->> 'name'
              FROM dgi_ask.cso_info i
                   JOIN dgi_prof.jsonstat j USING (sha256)
             WHERE j.extension -> 'copyright' ->> 'name' <> 'Central Statistics Office, Ireland'""").fetchall())


def qkey(question):
    return " ".join(textio.search_key(question).split()).rstrip("?!. ")


def create(question, provider, model, own_key, client, parent=None):
    aid = secrets.token_urlsafe(6)
    with db.connect() as conn:
        conn.execute("""
            INSERT INTO app.answer (id, question, qkey, status, provider, model, own_key, client, parent)
            VALUES (%s, %s, %s, 'working', %s, %s, %s, %s, %s)""",
            (aid, question, qkey(question), provider, model, own_key, client, parent))
    return aid


def get(aid):
    with db.connect(row_factory=dict_row) as conn:
        conn.execute(f"""
            UPDATE app.answer
               SET status = 'failed'
                 , error = 'interrupted'
                 , finished_at = now()
             WHERE id = %s
               AND status = 'working'
               AND created_at < now() - interval '{STALE_AFTER}'""", (aid,))
        return conn.execute("SELECT * FROM app.answer WHERE id = %s", (aid,)).fetchone()


def thread(aid, limit=6):
    """The finished answers a question follows on from, oldest first, ending with aid itself."""
    with db.connect(row_factory=dict_row) as conn:
        return conn.execute("""
            WITH RECURSIVE up AS (
                SELECT
                       a.*
                     , 1 AS depth
                  FROM app.answer a
                 WHERE a.id = %s
                 UNION ALL
                SELECT
                       p.*
                     , up.depth + 1
                  FROM app.answer p
                       JOIN up ON up.parent = p.id
                 WHERE up.depth < %s
            )
              SELECT *
                FROM up
               WHERE status = 'done'
            ORDER BY depth DESC""", (aid, limit)).fetchall()


def finish(aid, answer_md, tables, sources, steps, model, tokens_in, tokens_out, cost, seconds):
    with db.connect() as conn:
        conn.execute("""
            UPDATE app.answer
               SET status = 'done'
                 , answer_md = %s
                 , tables = %s
                 , sources = %s
                 , steps = %s
                 , model = %s
                 , tokens_in = %s
                 , tokens_out = %s
                 , cost_usd = %s
                 , seconds = %s
                 , finished_at = now()
             WHERE id = %s""",
            (answer_md, Jsonb(tables), Jsonb(sources), Jsonb(steps), model, tokens_in,
             tokens_out, cost, seconds, aid))


def fail(aid, error, steps=()):
    with db.connect() as conn:
        conn.execute("""
            UPDATE app.answer
               SET status = 'failed'
                 , error = %s
                 , steps = %s
                 , finished_at = now()
             WHERE id = %s""", (error[:500], Jsonb(list(steps)), aid))


def cached(question, provider, model, days=30):
    """A finished answer to the same question from the same model, if there is a recent one."""
    with db.connect() as conn:
        row = conn.execute(f"""
            SELECT id
              FROM app.answer
             WHERE qkey = %s
               AND status = 'done'
               AND NOT own_key
               AND parent IS NULL
               AND provider = %s
               AND coalesce(feedback, 0) >= 0
               AND created_at > now() - interval '{int(days)} days'
          ORDER BY created_at DESC
             LIMIT 1""", (qkey(question), provider)).fetchone()
    return row[0] if row else None


def asked_today(client):
    with db.connect() as conn:
        return conn.execute("""
            SELECT count(*)
              FROM app.answer
             WHERE client = %s
               AND NOT own_key
               AND created_at >= current_date""", (client,)).fetchone()[0]


def spent_this_month():
    with db.connect() as conn:
        return float(conn.execute("""
            SELECT coalesce(sum(cost_usd), 0)
              FROM app.answer
             WHERE NOT own_key
               AND created_at >= date_trunc('month', now())""").fetchone()[0])


def set_feedback(aid, vote):
    with db.connect() as conn:
        conn.execute("UPDATE app.answer SET feedback = %s WHERE id = %s", (vote, aid))

"""Phase 5: embeddings for meaning-based search.

What gets embedded (dgi_vec.item):
- a card per dataset: title (English and Irish), publisher, description,
  themes, keywords, time span, location keys, councils covered, files, column
  names with sample values, CSO dimensions and statistics, map layers;
- a card per map layer: its name, dataset, geometry, fields, sample values
  and the councils it covers, so map layers can be found on their own;
- chunks of about 750 tokens of document text (PDFs, web pages, metadata).

Model: text-embedding-3-large shortened to 1024 dimensions. Calls go directly
to the API in batches of up to 100 texts and are saved as they arrive, so an
interrupted run carries on. Items are only re-embedded when their text changes.
"""

import hashlib
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, textio

log = logging.getLogger("dgi")

MODEL = "text-embedding-3-large"
DIMS = 1024
MAX_TOKENS = 8000            # the model accepts 8,191
CHUNK_CHARS = 3000           # about 750 tokens
CHUNK_OVERLAP = 300
REQ_ITEMS = 100
REQ_TOKENS = 250_000         # per request, under the API's 300k limit
PRICE_PER_MTOK = 0.13        # USD, text-embedding-3-large, direct (not batch)


def _key():
    from .ask.llm import find_key
    key = find_key("OPENAI_API_KEY")
    if not key:
        raise SystemExit("no OPENAI_API_KEY in the environment, ~/.config/dgi-explore/keys.env "
                         "or ~/learnarr/.env")
    return key


def _enc():
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


def _trim(text, enc):
    toks = enc.encode(text, disallowed_special=())
    if len(toks) > MAX_TOKENS:
        return enc.decode(toks[:MAX_TOKENS]), MAX_TOKENS
    return text, len(toks)


def _lines(*parts):
    return "\n".join(p for p in parts if p)


def _join(xs, n=None, sep=", "):
    xs = [str(x) for x in xs if x not in (None, "")]
    if n and len(xs) > n:
        return sep.join(xs[:n]) + f"{sep}… ({len(xs)} in all)"
    return sep.join(xs)


# ── gathering ────────────────────────────────────────────────────────────────

def _gather(conn):
    q = lambda s, *a: conn.execute(s, a).fetchall()
    ds = {r[0]: r for r in q("""
        SELECT d.id, d.title, d.title_ga, d.description, p.name, d.themes, d.keywords,
               d.periodicity, d.temporal_start, d.temporal_end
          FROM dgi_cat.dataset d LEFT JOIN dgi_cat.publisher p ON p.id = d.publisher_id""")}
    la_name = dict(q("SELECT code, name FROM dgi_ref.area WHERE level = 'la'"))

    keys = defaultdict(list)
    for did, concept, level, starts, ends in q("""
            SELECT dataset_id, concept, level, starts, ends FROM dgi_link.dataset_key"""):
        keys[did].append((concept, level, starts, ends))

    places = defaultdict(set)
    for did, code in q("""
            SELECT DISTINCT r.dataset_id, e.la_code
              FROM dgi_link.layer_place_exact e
                   JOIN dgi_geo.layer_table t USING (table_name)
                   JOIN dgi_cat.resource r ON r.sha256 = t.sha256
             WHERE e.share >= 0.05"""):
        places[did].add(la_name.get(code, code))
    for did, code in q("SELECT dataset_id, la_code FROM dgi_link.dataset_place"):
        places[did].add(la_name.get(code, code))

    files = defaultdict(lambda: defaultdict(int))
    sha_ds = defaultdict(set)
    for did, sha, kind in q("""
            SELECT r.dataset_id, r.sha256, f.kind
              FROM dgi_cat.resource r JOIN dgi_prof.file f ON f.sha256 = r.sha256"""):
        files[did][kind] += 1
        sha_ds[sha].add(did)

    cols = defaultdict(list)
    for sha, name, typ, samples in q("""
            SELECT c.sha256, c.name, c.inferred_type, c.samples
              FROM dgi_prof.col c JOIN dgi_prof.file f USING (sha256)
             WHERE f.kind = 'table' ORDER BY c.sha256, c.part, c.ordinal"""):
        cols[sha].append((name, typ, samples))

    cso = {}
    for sha, label, dims, units, notes in q("""
            SELECT sha256, label, dims, units, notes FROM dgi_prof.jsonstat"""):
        cso[sha] = (label, dims, units, notes)

    layers = defaultdict(list)
    for sha, part, layer, gtype, n, fields, sample, tname in q("""
            SELECT l.sha256, l.part, l.layer, l.geom_type, l.feature_count, l.fields, l.sample, t.table_name
              FROM dgi_prof.layer l
                   LEFT JOIN dgi_geo.layer_table t USING (sha256, part, layer)
             WHERE l.geom_type IS NOT NULL AND l.geom_type NOT IN ('None')"""):
        layers[sha].append((part, layer, gtype, n, fields, sample, tname))

    layer_places = defaultdict(list)
    for tname, code, share in q("SELECT table_name, la_code, share FROM dgi_link.layer_place_exact ORDER BY share DESC"):
        layer_places[tname].append(la_name.get(code, code))

    docs = q("""
        SELECT d.sha256, d.title, d.text, f.kind
          FROM dgi_prof.doc_text d JOIN dgi_prof.file f USING (sha256)
         WHERE f.kind <> 'json' AND length(d.text) > 200""")
    return ds, keys, places, files, sha_ds, cols, cso, layers, layer_places, docs


def _time_text(k):
    times = [(lv, s, e) for c, lv, s, e in k if c == "time" and s]
    if not times:
        return None
    s = min(t[1] for t in times)
    e = max(t[2] for t in times)
    levels = sorted({t[0] for t in times})
    return f"Time: {s.year}–{e.year} ({', '.join(levels)})"


def _layer_desc(layer, gtype, n, fields, sample, places):
    fnames = [f[0] for f in (fields or [])]
    samp = ""
    if sample:
        vals = []
        for row in sample[:3]:
            # In the layer's own field order (jsonb sorts keys), skipping binary junk.
            order = [f for f in fnames if f in row] + [k for k in row if k not in fnames]
            vals.append("; ".join(f"{k}: {row[k]}" for k in order[:14]
                                  if row.get(k) and textio.printable(str(row[k]))))
        samp = "Sample features: " + " | ".join(v for v in vals if v)
    return _lines(
        f"Map layer: {layer} ({gtype or 'geometry'}, {n or '?'} features)",
        f"Fields: {_join(fnames, 40)}" if fnames else None,
        samp or None,
        f"Covers: {_join(places, 12)}" if places else None)


def _restorer(conn, ds, cols, docs):
    """Known words for restoring letters lost before publication (see textio.Restorer)."""
    texts = [t for row in ds.values() for t in (row[1], row[2], row[3], row[4])]
    texts += [r[0] for r in conn.execute("SELECT concat_ws(' ', name, name_ga) FROM dgi_ref.area "
                                         "WHERE level NOT IN ('sa')")]
    texts += [" ".join(s or []) for lst in cols.values() for _, _, s in lst]
    texts += [d[2] for d in docs]
    return textio.Restorer(texts)


def build_items(conn):
    ds, keys, places, files, sha_ds, cols, cso, layers, layer_places, docs = _gather(conn)
    enc = _enc()
    restorer = _restorer(conn, ds, cols, docs)
    restored = []
    items = []
    ds_shas = defaultdict(list)
    for sha, dids in sha_ds.items():
        for d in dids:
            ds_shas[d].append(sha)

    for did, (_, title, title_ga, desc, pub, themes, kws, period, ts, te) in ds.items():
        k = keys.get(did, [])
        geo = sorted({lv for c, lv, *_ in k if c == "geo"})
        parts = [
            f"Dataset: {title}",
            f"Irish title: {title_ga}" if title_ga else None,
            f"Publisher: {pub}" if pub else None,
            f"Description: {(desc or '')[:2000]}" if desc else None,
            f"Themes: {_join(themes)}" if themes else None,
            f"Keywords: {_join(kws, 30)}" if kws else None,
            f"Updated: {period}" if period else None,
            _time_text(k) or (f"Time: {ts}–{te}" if ts else None),
            f"Location levels: {_join(geo)}" if geo else None,
            f"Places covered: {_join(sorted(places[did]), 15)}" if places.get(did) else None,
            "Files: " + _join(f"{n} {kind}" for kind, n in sorted(files[did].items())) if files.get(did) else None,
        ]
        for sha in ds_shas.get(did, [])[:6]:
            if sha in cso:
                label, dims, units, notes = cso[sha]
                dim_txt = []
                for d in dims or []:
                    cats = list((d.get("categories") or {}).values())
                    dim_txt.append(f"{d.get('label')}: {_join(cats, 8 if d.get('id') != 'STATISTIC' else 20)}")
                parts.append(_lines(f"CSO table: {label}", *dim_txt,
                                    f"Units: {_join(sorted({u.get('label') for u in (units or {}).values() if isinstance(u, dict)}))}" if units else None))
            if sha in cols:
                c = cols[sha][:40]
                parts.append("Columns: " + _join(
                    f"{n}" + (f" (e.g. {', '.join(v for v in s[:3] if textio.printable(v))})"
                              if s and t in ('text', 'mixed') else f" [{t}]")
                    for n, t, s in c))
            for (part, layer, gtype, n, fields, sample, tname) in layers.get(sha, [])[:8]:
                parts.append(_layer_desc(layer, gtype, n, fields, sample, layer_places.get(tname)))
        body, ntok = _trim(_lines(*parts), enc)
        items.append(("ds:" + did, "dataset", did, None, title, body, ntok))

    # One card per map layer.
    for sha, lst in layers.items():
        dids = sorted(sha_ds.get(sha, []))
        if not dids:
            continue
        d = ds.get(dids[0])
        for (part, layer, gtype, n, fields, sample, tname) in lst:
            body = _lines(f"Map layer {layer} from the dataset {d[1] or 'without a title'}" if d
                          else f"Map layer {layer}",
                          f"Publisher: {d[4]}" if d and d[4] else None,
                          _layer_desc(layer, gtype, n, fields, sample, layer_places.get(tname)))
            body, ntok = _trim(body, enc)
            iid = "layer:" + (tname or f"{sha[:12]}:{part}:{layer}")
            items.append((iid, "layer", dids[0], sha, layer, body, ntok))

    # Document text in overlapping chunks.
    for sha, dtitle, text, kind in docs:
        dids = sorted(sha_ds.get(sha, []))
        d = ds.get(dids[0]) if dids else None
        head = _lines(f"From the dataset {d[1]}" if d else None, f"Document: {dtitle}" if dtitle else None)
        step = CHUNK_CHARS - CHUNK_OVERLAP
        for i, start in enumerate(range(0, len(text), step)):
            chunk = text[start:start + CHUNK_CHARS].strip()
            if len(chunk) < 200:
                continue
            body, ntok = _trim(_lines(head, chunk), enc)
            items.append((f"doc:{sha}:{i}", "doc", dids[0] if dids else None, sha, dtitle, body, ntok))

    # Letters the publishers' own files lost (U+FFFD): restore where unambiguous, log each one.
    out = []
    for iid, kind, did, sha, title, body, ntok in items:
        fixes = []
        new = body
        if textio.MOJIBAKE.search(new):
            # Line by line: given a whole document, the repair skips names
            # double-encoded inside otherwise correct text ("SeÃ¡n Kelly").
            new = "\n".join(textio.fix_mojibake(line) for line in new.split("\n"))
            if new != body:
                fixes.append((body, new))
        new = restorer.restore(new, fixes)
        title = restorer.restore(title) if title else title
        for before, after in fixes:
            for b, a in zip(before.splitlines(), after.splitlines()):
                if a != b:
                    restored.append((iid, b[:2000], a[:2000]))
        out.append((iid, kind, did, sha, title, new, ntok))
    build_items.restored = restored
    return out


def prepare(conn):
    """Write the items; keep existing embeddings where the text didn't change."""
    db.run_sql_files(conn, "50_vec")
    items = build_items(conn)
    conn.execute("CREATE TEMP TABLE new_item (LIKE dgi_vec.item) ON COMMIT DROP")
    with conn.cursor().copy("COPY new_item (item_id, kind, dataset_id, sha256, title, body, body_hash, tokens) "
                            "FROM STDIN") as cp:
        for iid, kind, did, sha, title, body, ntok in items:
            cp.write_row((iid, kind, did, sha, title, body.replace("\x00", ""),
                          hashlib.sha1(body.encode()).hexdigest(), ntok))
    conn.execute("DELETE FROM dgi_prof.text_fix WHERE source = 'restore'")
    with conn.cursor().copy("COPY dgi_prof.text_fix (source, ref, field, before, after) FROM STDIN") as cp:
        for iid, before, after in build_items.restored:
            cp.write_row(("restore", iid, "embedding text", before, after))
    log.info("restored lost letters in %d lines (logged in dgi_prof.text_fix, source 'restore')",
             len(build_items.restored))
    conn.execute("DELETE FROM dgi_vec.item i WHERE NOT EXISTS (SELECT FROM new_item n WHERE n.item_id = i.item_id)")
    conn.execute("""
        INSERT INTO dgi_vec.item (item_id, kind, dataset_id, sha256, title, body, body_hash, tokens)
          SELECT item_id, kind, dataset_id, sha256, title, body, body_hash, tokens FROM new_item
        ON CONFLICT (item_id) DO UPDATE
           SET kind = EXCLUDED.kind, dataset_id = EXCLUDED.dataset_id, sha256 = EXCLUDED.sha256,
               title = EXCLUDED.title, body = EXCLUDED.body, tokens = EXCLUDED.tokens,
               emb = CASE WHEN dgi_vec.item.body_hash = EXCLUDED.body_hash THEN dgi_vec.item.emb END,
               embedded_at = CASE WHEN dgi_vec.item.body_hash = EXCLUDED.body_hash THEN dgi_vec.item.embedded_at END,
               body_hash = EXCLUDED.body_hash""")
    conn.commit()
    return len(items)


# ── embedding ────────────────────────────────────────────────────────────────

def _requests(rows):
    """(batch, tokens) per API request."""
    batch, toks = [], 0
    for iid, body, ntok in rows:
        if batch and (len(batch) >= REQ_ITEMS or toks + ntok > REQ_TOKENS):
            yield batch, toks
            batch, toks = [], 0
        batch.append((iid, body))
        toks += ntok
    if batch:
        yield batch, toks


class Pacer:
    """Keep under the account's tokens-per-minute limit, across threads."""

    def __init__(self, tpm):
        import threading
        self.tpm, self.used, self.lock = tpm, [], threading.Lock()

    def wait(self, ntok):
        while True:
            with self.lock:
                now = time.time()
                self.used = [(t, n) for t, n in self.used if now - t < 60]
                if sum(n for _, n in self.used) + ntok <= self.tpm:
                    self.used.append((now, ntok))
                    return
            time.sleep(1)


def _embed_batch(client, batch, pacer=None, ntok=0):
    if pacer:
        pacer.wait(ntok)
    for attempt in range(20):
        try:
            r = client.embeddings.create(model=MODEL, input=[b for _, b in batch], dimensions=DIMS)
            return [(batch[d.index][0], d.embedding) for d in r.data], r.usage.total_tokens
        except Exception as e:
            wait = min(120, 5 * 2 ** attempt)
            log.warning("embedding request failed (%s); retrying in %ds", str(e)[:150], wait)
            time.sleep(wait)
    raise RuntimeError("embedding request kept failing")


def run(dry_run=False, prepare_only=False, threads=4, limit=None, tpm=900_000):
    with db.connect() as conn:
        n = prepare(conn)
        todo = conn.execute("SELECT item_id, body, tokens FROM dgi_vec.item WHERE emb IS NULL "
                            "ORDER BY kind, item_id").fetchall()
    if limit:
        todo = todo[:limit]
    tokens = sum(t for *_, t in todo)
    log.info("%d items in all; %d to embed, %.1fM tokens, about $%.2f", n, len(todo), tokens / 1e6,
             tokens / 1e6 * PRICE_PER_MTOK)
    if dry_run or prepare_only or not todo:
        return
    from openai import OpenAI
    client = OpenAI(api_key=_key(), max_retries=2)
    done = used = 0
    pacer = Pacer(tpm)
    with db.connect() as conn, ThreadPoolExecutor(threads) as pool:
        for results, ntok in pool.map(lambda bt: _embed_batch(client, bt[0], pacer, bt[1]), _requests(todo)):
            with conn.cursor() as cur:
                cur.executemany("UPDATE dgi_vec.item SET emb = %s, model = %s, embedded_at = now() WHERE item_id = %s",
                                [(vec, f"{MODEL}@{DIMS}", iid) for iid, vec in results])
            conn.commit()
            done += len(results)
            used += ntok
            if done % 2000 < len(results):
                log.info("embedded %d/%d items, %.1fM tokens, $%.2f so far", done, len(todo),
                         used / 1e6, used / 1e6 * PRICE_PER_MTOK)
    log.info("embedded %d items, %.1fM tokens, about $%.2f", done, used / 1e6, used / 1e6 * PRICE_PER_MTOK)
    similar()


def similar(k=20):
    """dgi_vec.similar: each dataset's k nearest datasets by meaning (numpy, no pgvector needed)."""
    import numpy as np
    with db.connect() as conn:
        db.run_sql_files(conn, "50_vec")
        rows = conn.execute("SELECT dataset_id, emb FROM dgi_vec.item WHERE kind = 'dataset' AND emb IS NOT NULL").fetchall()
        if not rows:
            log.info("no dataset embeddings yet")
            return
        ids = [r[0] for r in rows]
        m = np.asarray([r[1] for r in rows], dtype=np.float32)
        m /= np.linalg.norm(m, axis=1, keepdims=True)
        out = []
        for start in range(0, len(ids), 2000):
            sims = m[start:start + 2000] @ m.T
            for i, row in enumerate(sims):
                row[start + i] = -1                        # not itself
                top = np.argpartition(-row, k)[:k]
                top = top[np.argsort(-row[top])]
                for rank, j in enumerate(top, 1):
                    out.append((ids[start + i], rank, ids[j], float(row[j])))
        conn.execute("TRUNCATE dgi_vec.similar")
        with conn.cursor().copy("COPY dgi_vec.similar FROM STDIN") as cp:
            for r in out:
                cp.write_row(r)
        conn.commit()
    log.info("similar datasets: %d rows", len(out))

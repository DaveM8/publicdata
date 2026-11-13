"""Phase 6d: the semantic search page, http://scierstar/dgi/search.

Type a question in plain English (or Irish). The query is embedded with the
same model as the items, compared against every item in memory (numpy; about
45k vectors, a few milliseconds), and blended with Postgres full-text matches
(reciprocal rank fusion), so exact words and meaning both count. Results are
grouped by dataset and show the publisher, location level, years, and what
each dataset can be joined with.

Run: uv run uvicorn dgi_explore.search_app:app --host 127.0.0.1 --port 8002
(the systemd user service dgi-search does this).
"""

import html
import time
from collections import defaultdict

import numpy as np
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from . import db, embed, textio

app = FastAPI(title="dgi-explore search")
_state = {}
RELOAD_AFTER = 3600


def _load():
    if _state and time.time() - _state["at"] < RELOAD_AFTER:
        return _state
    with db.connect() as conn:
        rows = conn.execute("SELECT item_id, kind, dataset_id, title, emb FROM dgi_vec.item WHERE emb IS NOT NULL").fetchall()
        meta = {r[0]: r for r in conn.execute("""
            SELECT d.id, d.title, d.title_ga, p.name,
                   (SELECT string_agg(DISTINCT k.level, ', ') FROM dgi_link.dataset_key k
                     WHERE k.dataset_id = d.id AND k.concept = 'geo'),
                   (SELECT to_char(min(k.starts), 'YYYY') || '–' || to_char(max(k.ends), 'YYYY')
                      FROM dgi_link.dataset_key k WHERE k.dataset_id = d.id AND k.concept = 'time')
              FROM dgi_cat.dataset d LEFT JOIN dgi_cat.publisher p ON p.id = d.publisher_id""")}
    m = np.asarray([r[4] for r in rows], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    _state.update(at=time.time(), ids=[r[0] for r in rows], kinds=[r[1] for r in rows],
                  dsets=[r[2] for r in rows], titles=[r[3] for r in rows], m=m, meta=meta)
    from openai import OpenAI
    _state.setdefault("client", OpenAI(api_key=embed._key()))
    return _state


def _embed_query(st, q):
    r = st["client"].embeddings.create(model=embed.MODEL, input=[q], dimensions=embed.DIMS)
    v = np.asarray(r.data[0].embedding, dtype=np.float32)
    return v / np.linalg.norm(v)


def search(q, limit=25, kinds=None):
    st = _load()
    sims = st["m"] @ _embed_query(st, q)
    order = np.argsort(-sims)[:400]
    vec_rank = {}
    for rank, i in enumerate(order):
        if kinds and st["kinds"][i] not in kinds:
            continue
        d = st["dsets"][i]
        if d and d not in vec_rank:
            vec_rank[d] = (len(vec_rank), float(sims[i]), st["ids"][i])
    with db.connect() as conn:
        txt = conn.execute("""
            SELECT id
              FROM dgi_cat.dataset
             WHERE tsv @@ websearch_to_tsquery('english', dgi_cat.f_key(%s))
          ORDER BY ts_rank(tsv, websearch_to_tsquery('english', dgi_cat.f_key(%s))) DESC
             LIMIT 200""", (q, q)).fetchall()
    txt_rank = {r[0]: i for i, r in enumerate(txt)}
    # Reciprocal rank fusion: each list contributes 1 / (60 + rank).
    score = defaultdict(float)
    for d, (r, _, _) in vec_rank.items():
        score[d] += 1 / (60 + r)
    for d, r in txt_rank.items():
        score[d] += 1 / (60 + r)
    out = []
    for d in sorted(score, key=lambda x: -score[x])[:limit]:
        m = st["meta"].get(d)
        if not m:
            continue
        via = vec_rank.get(d, (None, None, None))[2] or ""
        out.append(dict(id=d, title=m[1], title_ga=m[2], publisher=m[3], geo=m[4], years=m[5],
                        meaning=round(vec_rank[d][1], 3) if d in vec_rank else None,
                        words=d in txt_rank,
                        matched=("map layer" if via.startswith("layer:") else "document text"
                                 if via.startswith("doc:") else "dataset description" if via else "")))
    return out


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search · data.gov.ie mirror</title><link rel="stylesheet" href="/dgi/style.css">
<style>form{{display:flex;gap:.5rem;flex-wrap:wrap}} input[type=search]{{flex:1;min-width:14rem;padding:.5rem;font:inherit}}
.r{{padding:.6rem 0;border-bottom:1px solid var(--line)}} .r .meta{{color:var(--muted);font-size:13px}}</style></head>
<body><header><a class="home" href="/dgi/index.html">data.gov.ie mirror</a><nav><a href="/dgi/publishers.html">Publishers</a>
<a href="/dgi/places.html">By location level</a></nav></header><main>
<h1>Search</h1>
<form method="get"><input type="search" name="q" value="{q}" placeholder="e.g. where are the bus stops in Galway; vacant houses by electoral division" autofocus>
<label><input type="checkbox" name="maps" value="1" {maps}> map layers only</label><button>Search</button></form>
<p class="intro">Finds datasets by meaning as well as by words: ask in plain English or Irish. Accents don't matter.</p>
{results}</main></body></html>"""


@app.get("/dgi/search", response_class=HTMLResponse)
def page(q: str = Query(""), maps: str = Query("")):
    q = textio.clean(q).strip()
    results = ""
    if q:
        rows = search(q, kinds={"layer"} if maps else None)
        parts = []
        for r in rows:
            e = lambda s: html.escape(s or "")
            parts.append(
                f'<div class="r"><a href="/dgi/d/{e(r["id"])}.html">{e(r["title"]) or "(no title)"}</a>'
                + (f'<div class="ga" lang="ga">{e(r["title_ga"])}</div>' if r["title_ga"] else "")
                + f'<div class="meta">{e(r["publisher"])}'
                + (f' · location: {e(r["geo"])}' if r["geo"] else "")
                + (f' · {e(r["years"])}' if r["years"] else "")
                + (f' · matched in {e(r["matched"])}' if r["matched"] else "")
                + ("" if not r["words"] else " · word match")
                + "</div></div>")
        results = "".join(parts) or "<p>No results.</p>"
    return PAGE.format(q=html.escape(q), maps="checked" if maps else "", results=results)


@app.get("/dgi/search.json")
def api(q: str, limit: int = 25, maps: bool = False):
    return JSONResponse(search(textio.clean(q), limit=limit, kinds={"layer"} if maps else None))

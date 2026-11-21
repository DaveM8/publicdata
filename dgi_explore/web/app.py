"""Release 1 of the public site: ask a question, get an answer with its source.

    uv run dgi-explore web            # http://127.0.0.1:8003/

Pages are made on the server (Jinja2). The only JavaScript is static/ask.js,
which shows the steps while an answer is being worked on; without it the page
refreshes itself until the answer is ready. Nothing is loaded from any other
site. Plan and decisions: ~/org/20261111_dgi-search-plan.org.

Settings (environment):
    DGI_WEB_ROOT             path prefix when served behind nginx, e.g. /ask
    DGI_WEB_PROVIDER         who answers for free (default: llm.DEFAULT_PROVIDER)
    DGI_WEB_MODEL            its model (default: the provider's usual one)
    DGI_WEB_EFFORT           low, medium or high (default: llm.DEFAULT_EFFORT)
    DGI_WEB_DAILY_LIMIT      free questions per person per day (default 20)
    DGI_WEB_MONTHLY_CAP_USD  the site's own spending per month (default 100)
    DGI_WEB_BETA             1 to show the test-version notice on every page
"""

import asyncio
import contextlib
import json
import re
import logging
import os
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from ..ask import llm
from . import chart, files, limits, proof, runner, store
from .messages import t

log = logging.getLogger("dgi")
HERE = Path(__file__).parent

ROOT = os.environ.get("DGI_WEB_ROOT", "").rstrip("/")
PROVIDER = os.environ.get("DGI_WEB_PROVIDER", llm.DEFAULT_PROVIDER)
MODEL = os.environ.get("DGI_WEB_MODEL") or llm.DEFAULT_MODELS.get(PROVIDER)
EFFORT = os.environ.get("DGI_WEB_EFFORT", llm.DEFAULT_EFFORT)
DAILY_LIMIT = int(os.environ.get("DGI_WEB_DAILY_LIMIT", "20"))
MONTHLY_CAP = float(os.environ.get("DGI_WEB_MONTHLY_CAP_USD", "100"))
# Spending cap per day, so one bad day can't use up the whole month (security
# review finding 2). Default: a fifth of the monthly cap.
DAILY_CAP = float(os.environ.get("DGI_WEB_DAILY_CAP_USD") or MONTHLY_CAP / 5)
MAX_QUESTION = 400
BETA = os.environ.get("DGI_WEB_BETA", "") not in ("", "0")
# Keep search engines and well-behaved crawlers out (robots.txt and a noindex
# header on every page). On by default; DGI_WEB_NOINDEX=0 at launch.
NOINDEX = os.environ.get("DGI_WEB_NOINDEX", "1") not in ("", "0")
# Ask each browser to solve a small puzzle before a question is sent (proof.py).
PROOF = os.environ.get("DGI_WEB_PROOF", "1") not in ("", "0")

# Who a question is sent to, said plainly on the page (open and honest, plan D3).
SERVICES = {
    "deepseek": "DeepSeek, a company in China",
    "kimi": "Moonshot AI (Kimi), a company in China",
    "anthropic": "Anthropic (Claude), a company in the United States",
    "openai": "OpenAI, a company in the United States",
}
OWN_KEY_PROVIDERS = {"anthropic": "Claude (Anthropic)", "openai": "OpenAI"}

_md = MarkdownIt("commonmark", {"html": False}).disable(["image"]).enable(["table"])

# Links the model may make: only to the CSO and data.gov.ie. Any other link in
# its text is shown as plain words, so a crafted question can't put a link to
# another site on a publicdata.ie page (security review finding 4). The
# source links under each answer are made by the site, not the model.
LINK_HOSTS = {"cso.ie", "www.cso.ie", "data.cso.ie", "data.gov.ie", "www.data.gov.ie"}


def _linked_ok(href):
    u = urllib.parse.urlsplit(href or "")
    return u.scheme == "https" and (u.hostname or "").lower() in LINK_HOSTS


def _drop_other_links(state):
    for block in state.tokens:
        for tok in block.children or []:
            if tok.type == "link_open":
                keep = _linked_ok(tok.attrGet("href"))
                if keep:
                    tok.attrSet("rel", "noopener nofollow")
            if tok.type in ("link_open", "link_close") and not keep:
                tok.type, tok.tag, tok.content, tok.attrs = "text", "", "", {}


_md.core.ruler.push("drop_other_links", _drop_other_links)


def _markdown(text):
    """The model's sentences as HTML. Any HTML in them is shown as text, never run."""
    return Markup(_md.render(text or ""))


def _num(v):
    if isinstance(v, bool) or v is None:
        return "" if v is None else str(v)
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


def _numeric_cols(table):
    cols = []
    for i in range(len(table["headings"])):
        vals = [r[i] for r in table["rows"] if r[i] is not None]
        cols.append(bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                       for v in vals))
    return cols


# Templates are read once, when the site starts, and never again: a git pull
# on the server must not give the running (old) code new templates. On 20 Nov
# that blanked every answer page until the site was restarted.
env = Environment(loader=FileSystemLoader(HERE / "templates"),
                  autoescape=select_autoescape(["html"]), auto_reload=False, cache_size=-1)
env.filters["markdown"] = _markdown
env.filters["num"] = _num
env.globals.update(t=t, root=ROOT, service=SERVICES.get(PROVIDER, PROVIDER), owner=store.owner,
                   own_key_providers=OWN_KEY_PROVIDERS, max_question=MAX_QUESTION, beta=BETA)


def page(name, status=200, **ctx):
    if PROOF:
        ctx.setdefault("proof", json.dumps(proof.challenge()))
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status)


def _address(request):
    """The visitor's address, from nginx (X-Real-IP). Used for limits, never stored."""
    return (request.headers.get("x-real-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def _client(request):
    return store.client_hash(_address(request))


def _url(request, path):
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}{ROOT}{path}"


router = APIRouter(prefix=ROOT)


@router.get("/", response_class=HTMLResponse)
def home(q: str = ""):
    return page("index.html", question=q[:MAX_QUESTION], error=None)


@router.get("/about", response_class=HTMLResponse)
def about():
    return page("about.html", daily_limit=DAILY_LIMIT)


def _turn(row):
    """An earlier answer, as the context a follow-up question is read with."""
    return {"question": row["question"], "answer": row["answer_md"],
            "tables": [tb["title"] for tb in row["tables"]],
            "queries": [s["sql"] for s in row["steps"]
                        if s.get("tool") == "run_sql" and s.get("sql") and not s.get("error")]}


@router.post("/ask")
async def ask(request: Request):
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    get = lambda k: (form.get(k) or [""])[0].strip()
    question = " ".join(get("question").split())
    own_provider, own_model, own_key = get("provider"), get("model"), get("key")
    # A follow-up names the answer it follows on from; an unknown one is ignored.
    turns = store.thread(get("after")) if get("after") else []
    parent = turns[-1]["id"] if turns and turns[-1]["id"] == get("after") else None
    context = [_turn(r) for r in turns] if parent else None

    def again(message, status=400):
        return page("index.html", status=status, question=question[:MAX_QUESTION], error=message,
                    after=parent)

    if not question:
        return again(t.err_empty)
    if len(question) > MAX_QUESTION:
        return again(t.err_long.format(n=MAX_QUESTION))

    def stopped():
        """The bot puzzle, then the per-minute limit (an IPv6 /64 counts as one visitor)."""
        if PROOF and not proof.check(get("proof_salt"), get("proof_challenge"),
                                     get("proof_signature"), get("proof_number")):
            return again(t.err_proof, status=403)
        if not limits.may_ask(limits.visitor(_address(request))):
            return again(t.err_rate, status=429)
        return None

    client = _client(request)
    if own_key or own_provider in OWN_KEY_PROVIDERS:
        if not own_key or own_provider not in OWN_KEY_PROVIDERS:
            return again(t.err_key)
        provider, model, key = own_provider, own_model or llm.DEFAULT_MODELS.get(own_provider), own_key
        if not model:
            return again(t.err_key)
        if (no := stopped()):
            return no
    else:
        if (no := stopped()):
            return no
        provider, model, key = PROVIDER, MODEL, None
        hit = None if parent else store.cached(question, provider, model)
        if hit:
            return RedirectResponse(f"{ROOT}/a/{hit}", status_code=303)
        if store.asked_today(client) >= DAILY_LIMIT:
            return again(t.err_daily.format(n=DAILY_LIMIT), status=429)
        if store.spent_this_month() >= MONTHLY_CAP:
            return again(t.err_month, status=429)
        if store.spent_today() >= DAILY_CAP:
            return again(t.err_today, status=429)

    aid = store.create(question, provider, model, bool(key), client, parent)
    runner.start(aid, question, provider, model, EFFORT, key, context)
    # A follow-up opens at the new question, below the conversation so far.
    return RedirectResponse(f"{ROOT}/a/{aid}" + (f"#t-{aid}" if parent else ""), status_code=303)


SHOW_TURNS = 50          # earlier answers shown above a follow-up (the model is given fewer)


def _view(row):
    """A finished answer, ready for _turn.html."""
    return {"row": row, "when": row["finished_at"].strftime("%-d %B %Y"),
            "tables": [dict(tb, numeric=_numeric_cols(tb), chart_spec=chart.spec(tb))
                       for tb in row["tables"]]}


@router.get("/a/{aid}", response_class=HTMLResponse)
def answer(aid: str):
    row = store.get(aid)
    if not row:
        return page("notfound.html", status=404)
    # A follow-up's page shows the whole conversation, oldest first.
    earlier = [_view(r) for r in store.thread(row["parent"], SHOW_TURNS)] if row["parent"] else []
    if row["status"] == "working":
        j = runner.job(aid)
        return page("working.html", row=row, earlier=earlier,
                    steps=list(j.events) if j else [t.working_first])
    if row["status"] == "failed":
        reason = {"setup": t.failed_setup, "interrupted": t.failed_interrupted}.get(
            row["error"], t.failed_generic)
        return page("failed.html", row=row, reason=reason, earlier=earlier)
    return page("answer.html", row=row, turns=earlier + [_view(row)])


EVENTS_POLL = 0.5          # seconds between looks at an answer's progress
EVENTS_MAX = 15 * 60       # give up on a stream after this long


@router.get("/a/{aid}/events")
async def events(aid: str, request: Request):
    """The steps of an answer in progress, as server-sent events.

    Async on purpose: a waiting stream holds no worker thread, so open
    connections can't starve the ordinary pages (security review finding 1),
    and each visitor may hold only a few at once."""
    j = runner.job(aid)
    if not j:
        return Response("event: done\ndata: {}\n\n", media_type="text/event-stream")
    key = limits.visitor(_address(request))
    if not limits.open_events(key):
        # The page then falls back to reloading itself every few seconds.
        return Response("too many open connections", status_code=429, media_type="text/plain")

    async def stream():
        try:
            sent, quiet, began = 0, 0, asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - began < EVENTS_MAX:
                new, done = j.events[sent:], j.done
                for e in new:
                    yield f"data: {json.dumps(e)}\n\n"
                sent += len(new)
                if done and sent >= len(j.events):
                    yield "event: done\ndata: {}\n\n"
                    return
                quiet = 0 if new else quiet + 1
                if quiet and quiet % 30 == 0:
                    yield ": still working\n\n"
                await asyncio.sleep(EVENTS_POLL)
        finally:
            limits.close_events(key)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _table(aid, n):
    row = store.get(aid)
    if not row or row["status"] != "done" or not (1 <= n <= len(row["tables"])):
        return None, None
    return row, row["tables"][n - 1]


@router.get("/a/{aid}/table/{n}.xlsx")
def table_xlsx(aid: str, n: int, request: Request):
    row, table = _table(aid, n)
    if not table:
        return page("notfound.html", status=404)
    return Response(files.xlsx(row, table, _url(request, f"/a/{aid}")),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{files.filename(table, "xlsx")}"'})


@router.get("/a/{aid}/table/{n}.csv")
def table_csv(aid: str, n: int):
    row, table = _table(aid, n)
    if not table:
        return page("notfound.html", status=404)
    return Response(files.csv_bytes(table), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{files.filename(table, "csv")}"'})


@router.post("/a/{aid}/feedback")
async def feedback(aid: str, request: Request):
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    vote = {"yes": 1, "no": -1}.get((form.get("vote") or [""])[0])
    if vote and store.get(aid):
        store.set_feedback(aid, vote)
    # Back to the page the reply was given on, which may be a later answer in
    # the same conversation; only to an answer that exists.
    back = (form.get("back") or [""])[0]
    back = back if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", back) and store.get(back) else aid
    return RedirectResponse(f"{ROOT}/a/{back}#feedback-{aid}", status_code=303)


@contextlib.asynccontextmanager
async def _lifespan(app):
    for name in env.list_templates():
        env.get_template(name)
    store.setup()
    log.info("web: answers from %s %s, %d free questions a day, cap %.0f USD a month, root %r",
             PROVIDER, MODEL, DAILY_LIMIT, MONTHLY_CAP, ROOT)
    yield


app = FastAPI(title="Ask about Ireland", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if NOINDEX:
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return resp


@app.get("/robots.txt", response_class=Response)
def robots():
    """At the root of the domain, wherever the site itself is (ROOT)."""
    body = "User-agent: *\nDisallow: /\n" if NOINDEX else "User-agent: *\nDisallow: /ask\n"
    return Response(body, media_type="text/plain")


app.include_router(router)
app.mount(f"{ROOT}/static", StaticFiles(directory=HERE / "static"), name="static")

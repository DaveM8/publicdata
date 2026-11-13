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
"""

import contextlib
import json
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
from . import files, runner, store
from .messages import t

log = logging.getLogger("dgi")
HERE = Path(__file__).parent

ROOT = os.environ.get("DGI_WEB_ROOT", "").rstrip("/")
PROVIDER = os.environ.get("DGI_WEB_PROVIDER", llm.DEFAULT_PROVIDER)
MODEL = os.environ.get("DGI_WEB_MODEL") or llm.DEFAULT_MODELS.get(PROVIDER)
EFFORT = os.environ.get("DGI_WEB_EFFORT", llm.DEFAULT_EFFORT)
DAILY_LIMIT = int(os.environ.get("DGI_WEB_DAILY_LIMIT", "20"))
MONTHLY_CAP = float(os.environ.get("DGI_WEB_MONTHLY_CAP_USD", "100"))
MAX_QUESTION = 400

# Who a question is sent to, said plainly on the page (open and honest, plan D3).
SERVICES = {
    "deepseek": "DeepSeek, a company in China",
    "kimi": "Moonshot AI (Kimi), a company in China",
    "anthropic": "Anthropic (Claude), a company in the United States",
    "openai": "OpenAI, a company in the United States",
}
OWN_KEY_PROVIDERS = {"anthropic": "Claude (Anthropic)", "openai": "OpenAI"}

_md = MarkdownIt("commonmark", {"html": False}).disable(["image"]).enable(["table"])


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


env = Environment(loader=FileSystemLoader(HERE / "templates"),
                  autoescape=select_autoescape(["html"]))
env.filters["markdown"] = _markdown
env.filters["num"] = _num
env.globals.update(t=t, root=ROOT, service=SERVICES.get(PROVIDER, PROVIDER),
                   own_key_providers=OWN_KEY_PROVIDERS, max_question=MAX_QUESTION)


def page(name, status=200, **ctx):
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status)


def _client(request):
    address = (request.headers.get("x-real-ip")
               or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
               or (request.client.host if request.client else "unknown"))
    return store.client_hash(address)


def _url(request, path):
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}{ROOT}{path}"


router = APIRouter(prefix=ROOT)


@router.get("/", response_class=HTMLResponse)
def home(q: str = ""):
    return page("index.html", question=q[:MAX_QUESTION], error=None)


@router.get("/about", response_class=HTMLResponse)
def about():
    return page("about.html", daily_limit=DAILY_LIMIT)


@router.post("/ask")
async def ask(request: Request):
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    get = lambda k: (form.get(k) or [""])[0].strip()
    question = " ".join(get("question").split())
    own_provider, own_model, own_key = get("provider"), get("model"), get("key")

    def again(message, status=400):
        return page("index.html", status=status, question=question[:MAX_QUESTION], error=message)

    if not question:
        return again(t.err_empty)
    if len(question) > MAX_QUESTION:
        return again(t.err_long.format(n=MAX_QUESTION))

    client = _client(request)
    if own_key or own_provider in OWN_KEY_PROVIDERS:
        if not own_key or own_provider not in OWN_KEY_PROVIDERS:
            return again(t.err_key)
        provider, model, key = own_provider, own_model or llm.DEFAULT_MODELS.get(own_provider), own_key
        if not model:
            return again(t.err_key)
    else:
        provider, model, key = PROVIDER, MODEL, None
        hit = store.cached(question, provider, model)
        if hit:
            return RedirectResponse(f"{ROOT}/a/{hit}", status_code=303)
        if store.asked_today(client) >= DAILY_LIMIT:
            return again(t.err_daily.format(n=DAILY_LIMIT), status=429)
        if store.spent_this_month() >= MONTHLY_CAP:
            return again(t.err_month, status=429)

    aid = store.create(question, provider, model, bool(key), client)
    runner.start(aid, question, provider, model, EFFORT, key)
    return RedirectResponse(f"{ROOT}/a/{aid}", status_code=303)


@router.get("/a/{aid}", response_class=HTMLResponse)
def answer(aid: str, thanks: int = 0):
    row = store.get(aid)
    if not row:
        return page("notfound.html", status=404)
    if row["status"] == "working":
        j = runner.job(aid)
        return page("working.html", row=row, steps=list(j.events) if j else [t.working_first])
    if row["status"] == "failed":
        reason = {"setup": t.failed_setup, "interrupted": t.failed_interrupted}.get(
            row["error"], t.failed_generic)
        return page("failed.html", row=row, reason=reason)
    tables = [dict(tb, numeric=_numeric_cols(tb)) for tb in row["tables"]]
    return page("answer.html", row=row, tables=tables, thanks=bool(thanks),
                when=row["finished_at"].strftime("%-d %B %Y"))


@router.get("/a/{aid}/events")
def events(aid: str):
    """The steps of an answer in progress, as server-sent events."""
    j = runner.job(aid)

    def stream():
        if not j:
            yield "event: done\ndata: {}\n\n"
            return
        sent = 0
        while True:
            with j.cond:
                if sent >= len(j.events) and not j.done:
                    j.cond.wait(timeout=15)
                new, done = j.events[sent:], j.done
            for e in new:
                yield f"data: {json.dumps(e)}\n\n"
            sent += len(new)
            if done and sent >= len(j.events):
                yield "event: done\ndata: {}\n\n"
                return
            if not new:
                yield ": still working\n\n"

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
    return RedirectResponse(f"{ROOT}/a/{aid}?thanks=1#feedback", status_code=303)


@contextlib.asynccontextmanager
async def _lifespan(app):
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
    return resp


app.include_router(router)
app.mount(f"{ROOT}/static", StaticFiles(directory=HERE / "static"), name="static")

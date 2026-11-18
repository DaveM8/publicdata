"""Answer a question: give the model the tools and loop until it has an answer.

    uv run dgi-explore ask "how many people live in Leitrim?"
    uv run dgi-explore ask --steps "..."      # show each tool call

Every question, its tool calls and its answer are appended to
~/.local/state/dgi-explore/ask.jsonl. Plan: ~/org/20261111_dgi-search-plan.org.
"""

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import db
from . import llm, tools

log = logging.getLogger("dgi")

MAX_ROUNDS = 14              # tool rounds before giving up
MAX_RESULT_CHARS = 30_000    # a tool result longer than this is cut, and says so

SYSTEM = """\
You answer questions about Ireland for members of the public, using official \
statistics from the Central Statistics Office (CSO) held in a database. The \
people asking are not technical. They want a clear, correct answer and to know \
where it came from.

How to work: find candidate tables with search_tables, read the one you choose \
with describe_table so you use its exact column names and category text, then \
get the figures with run_sql. Check that the rows you got are the rows the \
question needs before you answer.

Accuracy matters more than anything else here, because people will repeat what \
you tell them.
- Every number in your answer must come from a run_sql result in this \
conversation. Never give a figure from memory, not even one you are sure of. \
You may do simple arithmetic on returned figures (a difference, a percentage), \
and say that you did.
- State the year or period, the unit, and the area each figure refers to.
- If the data does not hold what was asked, say so plainly and offer the \
closest thing it does hold (an earlier year, a wider area, a related measure), \
making clear how it differs from what was asked. A truthful "the data does not \
have this" is a good answer; a guess is not.
- Watch for totals stored beside their parts ('Both sexes', 'All ages', \
'State' or 'Ireland'), for a county against its city and county councils \
(Cork, Galway, Dublin's four councils), and for tables that cover only some \
years. If a place or term in the question could mean two things, say which \
one you used.
- The data is a copy taken in October 2026; each table shows when the CSO \
last updated it.

How to answer: start with the answer itself in one or two plain sentences, \
then anything the reader needs to understand it. Use everyday words; do not \
mention SQL, table codes or column names. Keep it short.

Tables: when the answer involves three or more figures of the same kind (a \
ranking, a comparison between places, a run of years), show them with \
show_table, giving the result_id of a run_sql result; the page draws the \
table from those exact rows. Do not type such figures out yourself as a \
table or a list, because retyping is where numbers go wrong. In your \
sentences, mention only the one or two figures that answer the question. \
Run a query that returns just the rows and columns worth showing, in a \
sensible order, before you call show_table. One table is usually enough.

Charts: show_table can also draw a chart above the table, from the same rows. \
Ask for chart "line" when the rows are a run of years or other periods in \
time order: the first column must be the period and every other column a \
figure in the same unit (at most four such columns, one line each). Ask for \
chart "bar" when the rows compare places or groups on one figure: at most 35 \
rows, sorted by the figure. Leave it at "none" when there are only one or two \
figures, when the columns are in different units, or when a picture would \
add nothing. If the table has figure columns that should not be drawn (a \
count of quarters, a total beside a percentage), name the ones to draw in \
chart_columns.

Follow-up questions: a question may come with earlier questions and answers \
from the same conversation. Use them to understand what is being asked now \
("and for Mayo?", "what about ten years ago?"). The tables used before are \
named, so you can go straight to them. Run a fresh query for every figure \
you give now, including ones repeated from an earlier answer.

Say only what you have checked. Do not state what an area includes or leaves \
out (for example whether a county figure includes its city), or how a figure \
was collected, unless the table's notes or a query result in this \
conversation shows it. If it matters and you could not check it, say that it \
is unclear.

Sources: do not write a source line or any links. The page lists, under your \
answer, every CSO table you queried, with its title, year and link.\
"""

MAX_TABLES = 3

SHOW_TABLE = {
    "name": "show_table",
    "description": (
        "Put a table on the answer page, drawn from the rows of an earlier run_sql result. "
        "Give that result's result_id, a short plain title saying what the table shows "
        "(with the year and unit), and a plain-word heading for each column, in order. "
        f"At most {MAX_TABLES} tables per answer."),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_id": {"type": "string", "description": "The result_id returned by run_sql, e.g. r2."},
            "title": {"type": "string"},
            "headings": {"type": "array", "items": {"type": "string"},
                         "description": "One heading per column of the result, in order."},
            "chart": {"type": "string", "enum": ["none", "bar", "line"],
                      "description": "A chart to draw above the table from the same rows. "
                                     "line: first column is the period, the other columns are "
                                     "figures in one unit. bar: places or groups compared on "
                                     "one figure."},
            "chart_columns": {"type": "array", "items": {"type": "string"},
                              "description": "The headings of the columns to draw, when not every "
                                             "figure column belongs in the chart (a count of "
                                             "quarters, a total beside a share). One for a bar "
                                             "chart; up to four, in one unit, for a line chart."},
        },
        "required": ["result_id", "title", "headings"],
        "additionalProperties": False,
    },
}


@dataclass
class Answer:
    question: str
    text: str = ""
    steps: list = field(default_factory=list)     # [{tool, input, result}]
    tables: list = field(default_factory=list)    # [{title, headings, columns, rows, sql, chart}]
    sources: list = field(default_factory=list)   # the CSO tables queried (tools.sources_for)
    stop: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0

    @property
    def cost(self):
        return llm.cost_usd(self.model, self.tokens_in, self.tokens_out)


def _with_context(question, context):
    """The first message of a follow-up: what went before, then the new question."""
    parts = ["Earlier in this conversation:"]
    for i, c in enumerate(context, 1):
        parts.append(f"\nQuestion {i}: {c['question']}\nAnswer {i}: {(c.get('answer') or '')[:3000]}")
        for title in c.get("tables") or []:
            parts.append(f"A table was shown with it: {title}")
        for q in (c.get("queries") or [])[-4:]:
            parts.append(f"A query that was used for it:\n{q[:1500]}")
    parts.append("\nThe question to answer now, which follows on from the above:\n" + question)
    return "\n".join(parts)


def answer(question, provider=None, model=None, effort=None, api_key=None, on_step=None,
           context=None):
    """Run the loop for one question. on_step(step) is called after each tool call.
    context: for a follow-up, the earlier turns, oldest first, as
    [{question, answer, tables: [title], queries: [sql]}]."""
    t0 = time.time()
    a = Answer(question=question)
    chat = llm.open_chat(SYSTEM, tools.SPECS + [SHOW_TABLE], provider=provider, model=model,
                         effort=effort, api_key=api_key)
    queries = {}       # result_id -> {sql, columns, rows}: every successful query
    reply = chat.user(_with_context(question, context) if context else question)
    rounds = 0
    while True:
        a.tokens_in += reply.tokens_in
        a.tokens_out += reply.tokens_out
        a.model = reply.model or a.model
        if not reply.tool_calls:
            break
        rounds += 1
        results = []
        for call in reply.tool_calls:
            if "_bad_json" in call.input:
                out = {"error": "the arguments were not valid JSON; send them again"}
            elif call.name == "show_table":
                out = _show_table(a, queries, call.input)
            else:
                out = tools.call(call.name, call.input)
                if call.name == "run_sql" and "rows" in out:
                    rid = f"r{len(queries) + 1}"
                    queries[rid] = {"sql": call.input.get("sql", ""), "columns": out["columns"],
                                    "rows": out["rows"]}
                    out = {"result_id": rid, **out}
            text = json.dumps(out, ensure_ascii=False, default=str)
            if len(text) > MAX_RESULT_CHARS:
                text = text[:MAX_RESULT_CHARS] + " ...[cut: result too long, ask for less]"
            step = {"tool": call.name, "input": call.input, "result": out}
            a.steps.append(step)
            if on_step:
                on_step(step)
            results.append((call.id, text, "error" in out))
        if rounds >= MAX_ROUNDS:
            results[-1] = (results[-1][0], results[-1][1] +
                           "\n[This was the last tool call allowed. Answer now with what you "
                           "have, or say what could not be found.]", results[-1][2])
        reply = chat.tool_results(results)
        if rounds > MAX_ROUNDS:
            a.tokens_in += reply.tokens_in
            a.tokens_out += reply.tokens_out
            break
    text = _without_source_lines(reply.text)
    if not text and reply.stop == "done":
        # Some models stop after their last tool call having written nothing, or
        # only a source line. Ask once for the answer itself.
        reply = chat.user("You have not written the answer for the reader yet. Write it now, "
                          "in plain words, using only the results above.")
        a.tokens_in += reply.tokens_in
        a.tokens_out += reply.tokens_out
        text = _without_source_lines(reply.text)
    a.text, a.stop = text, reply.stop
    if reply.stop == "refused":
        a.text = a.text or "The model declined to answer this question."
    try:
        a.sources = tools.sources_for([r["sql"] for r in queries.values()])
    except Exception:
        log.exception("could not list the sources")
    a.seconds = time.time() - t0
    _log(a)
    return a


def _without_source_lines(text):
    """Drop paragraphs that only name sources: the page lists the sources itself,
    from the tables that were really queried."""
    paras = [p for p in re.split(r"\n\s*\n", text or "")
             if not re.match(r"\s*\**\s*sources?\s*:", p, re.I)]
    return "\n\n".join(paras).strip()


def _show_table(a, results, args):
    """The show_table tool: mark a query result for display. The rows are ours, not the model's."""
    r = results.get(str(args.get("result_id", "")).strip())
    if not r:
        return {"error": "no such result_id; use one returned by run_sql in this conversation"}
    if not r["rows"]:
        return {"error": "that result has no rows"}
    if len(a.tables) >= MAX_TABLES:
        return {"error": f"an answer can show at most {MAX_TABLES} tables"}
    headings = [str(h) for h in (args.get("headings") or [])]
    if len(headings) != len(r["columns"]):
        return {"error": f"give exactly {len(r['columns'])} headings, one for each of: "
                         + ", ".join(r["columns"])}
    a.tables.append({"title": str(args.get("title") or "").strip() or "Table",
                     "headings": headings, "columns": r["columns"], "rows": r["rows"],
                     "sql": r["sql"],
                     "chart": args.get("chart") if args.get("chart") in ("bar", "line") else None,
                     "chart_columns": [str(h) for h in (args.get("chart_columns") or [])]})
    return {"ok": True, "rows_shown": len(r["rows"])}


def _log(a):
    db.STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "question": a.question, "answer": a.text, "stop": a.stop, "model": a.model,
           "tokens_in": a.tokens_in, "tokens_out": a.tokens_out, "cost_usd": a.cost,
           "seconds": round(a.seconds, 1),
           "tables": [{"title": t["title"], "rows": len(t["rows"])} for t in a.tables],
           "sources": [x["code"] for x in a.sources],
           "steps": [{"tool": s["tool"], "input": s["input"],
                      "error": s["result"].get("error")} for s in a.steps]}
    with open(db.STATE_DIR / "ask.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _show_step(step):
    arg = step["input"].get("sql") or json.dumps(step["input"], ensure_ascii=False)
    res = step["result"]
    if "error" in res:
        summary = "ERROR " + res["error"]
    elif "rows" in res:
        summary = f'{len(res["rows"])} rows: ' + json.dumps(res["rows"][:5], ensure_ascii=False)
    elif "tables" in res:
        summary = ", ".join(t["table"] for t in res["tables"])
    else:
        summary = json.dumps(res, ensure_ascii=False)
    print(f'  · {step["tool"]}: {arg}\n    → {summary[:400]}', file=sys.stderr, flush=True)


def cli(question, show_steps=False, provider=None, model=None, effort=None):
    a = answer(question, provider=provider, model=model, effort=effort,
               on_step=_show_step if show_steps else None)
    print(a.text)
    for t in a.tables:
        print(f"\n{t['title']}\n" + " | ".join(t["headings"]))
        for row in t["rows"]:
            print(" | ".join("" if v is None else f"{v:,.10g}" if isinstance(v, (int, float))
                             else str(v) for v in row))
    if a.sources:
        print("\nSources:")
        for x in a.sources:
            print(f"  {x['title']} ({x['code']}), {x['period']}, CSO, updated {x['updated']}: {x['link']}")
    cost = f"about ${a.cost:.3f}" if a.cost is not None else "cost unknown"
    print(f"\n[{a.model}, {len(a.steps)} tool calls, {a.seconds:.0f}s, "
          f"{a.tokens_in:,} tokens in, {a.tokens_out:,} out, {cost}]", file=sys.stderr)

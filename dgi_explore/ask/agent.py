"""Answer a question: give the model the tools and loop until it has an answer.

    uv run dgi-explore ask "how many people live in Leitrim?"
    uv run dgi-explore ask --steps "..."      # show each tool call

Every question, its tool calls and its answer are appended to
~/.local/state/dgi-explore/ask.jsonl. Plan: ~/org/20261111_dgi-search-plan.org.
"""

import json
import logging
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

How to answer: start with the answer itself in one or two plain sentences. \
Add a small table only when several figures are being compared. Use everyday \
words; do not mention SQL, table codes or column names in the answer. Finish \
with a line beginning "Source:" that names the CSO table by its title and \
code, the year of the data and its link.\
"""


@dataclass
class Answer:
    question: str
    text: str = ""
    steps: list = field(default_factory=list)     # [{tool, input, result}]
    stop: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0

    @property
    def cost(self):
        return llm.cost_usd(self.model, self.tokens_in, self.tokens_out)


def answer(question, provider=None, model=None, effort=None, api_key=None, on_step=None):
    """Run the loop for one question. on_step(step) is called after each tool call."""
    t0 = time.time()
    a = Answer(question=question)
    chat = llm.open_chat(SYSTEM, tools.SPECS, provider=provider, model=model, effort=effort,
                         api_key=api_key)
    reply = chat.user(question)
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
            out = ({"error": "the arguments were not valid JSON; send them again"}
                   if "_bad_json" in call.input else tools.call(call.name, call.input))
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
    a.text, a.stop = reply.text, reply.stop
    if reply.stop == "refused":
        a.text = a.text or "The model declined to answer this question."
    a.seconds = time.time() - t0
    _log(a)
    return a


def _log(a):
    db.STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "question": a.question, "answer": a.text, "stop": a.stop, "model": a.model,
           "tokens_in": a.tokens_in, "tokens_out": a.tokens_out, "cost_usd": a.cost,
           "seconds": round(a.seconds, 1),
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
    cost = f"about ${a.cost:.3f}" if a.cost is not None else "cost unknown"
    print(f"\n[{a.model}, {len(a.steps)} tool calls, {a.seconds:.0f}s, "
          f"{a.tokens_in:,} tokens in, {a.tokens_out:,} out, {cost}]", file=sys.stderr)

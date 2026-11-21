"""Runs each question in a background thread and lets the answer page follow it.

One web process holds the jobs in memory. If the process is restarted, a job
in flight is lost and its row is marked failed when next looked at (store.get).
"""

import logging
import threading

from ..ask import agent, llm
from . import store
from .messages import t

log = logging.getLogger("dgi")

MAX_RUNNING = 4          # questions being answered at once; the rest wait their turn
_slots = threading.Semaphore(MAX_RUNNING)
_jobs = {}               # answer id -> Job
_lock = threading.Lock()


class Job:
    def __init__(self):
        self.events = [t.working_first]     # plain-word steps, in order
        self.done = False
        self.cond = threading.Condition()

    def say(self, text):
        with self.cond:
            self.events.append(text)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()


def job(aid):
    return _jobs.get(aid)


def describe(step):
    """One tool call in plain words."""
    tool, args, res = step["tool"], step["input"], step["result"]
    if tool == "search_tables":
        return t.step_search.format(query=args.get("query", ""))
    if tool == "describe_table":
        title = res.get("title") or args.get("table", "")
        if args.get("find"):
            return t.step_describe_find.format(title=title, find=args["find"])
        return t.step_describe.format(title=title)
    if tool == "find_place":
        return t.step_place.format(name=args.get("name", ""))
    if tool == "run_sql":
        return t.step_sql_error if "error" in res else t.step_sql
    if tool == "show_table":
        return t.step_table.format(title=args.get("title", ""))
    return tool


def _run(aid, question, provider, model, effort, api_key, context):
    j = _jobs[aid]
    steps = []
    if not _slots.acquire(blocking=False):
        j.say(t.err_busy)
        _slots.acquire()
    try:
        def on_step(step):
            say = describe(step)
            steps.append({"say": say, "tool": step["tool"], "sql": step["input"].get("sql"),
                          "error": step["result"].get("error")})
            j.say(say)

        a = agent.answer(question, provider=provider, model=model, effort=effort,
                         api_key=api_key, on_step=on_step, context=context)
        cost = 0 if api_key else (a.cost or 0)
        if a.stop in ("done", "end_turn") and a.text:
            store.finish(aid, a.text, a.tables, a.sources, steps, a.model, a.tokens_in,
                         a.tokens_out, cost, a.seconds)
        else:
            store.fail(aid, f"stopped: {a.stop or 'no answer'}", steps)
    except SystemExit as e:               # no key, unknown provider: a set-up problem
        log.error("answer %s: %s", aid, "own key not usable" if api_key else e)
        store.fail(aid, "setup", steps)
    except Exception as e:
        if api_key:
            # A provider's error text can quote part of the key, and a person's
            # own key is never stored or logged: keep only the kind of error
            # (security review finding 8).
            log.error("answer %s failed with the person's own key: %s", aid, type(e).__name__)
            store.fail(aid, type(e).__name__, steps)
        else:
            log.exception("answer %s failed", aid)
            store.fail(aid, f"{type(e).__name__}: {e}", steps)
    finally:
        _slots.release()
        j.finish()
        # Keep the finished job briefly so a page that is still listening hears the end.
        threading.Timer(120, lambda: _jobs.pop(aid, None)).start()


def start(aid, question, provider, model, effort=None, api_key=None, context=None):
    """api_key is a person's own key. It lives only in this thread's memory.
    context: the earlier questions of the conversation (see agent.answer)."""
    with _lock:
        _jobs[aid] = Job()
    threading.Thread(target=_run, args=(aid, question, provider, model, effort, api_key, context),
                     name=f"answer-{aid}", daemon=True).start()


def default_model(provider):
    return llm.DEFAULT_MODELS.get(provider)

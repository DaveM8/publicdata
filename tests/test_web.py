"""The Ask site, without calling any model: pages, limits on input, downloads."""

import io

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from dgi_explore.web import app as webapp, files  # noqa: E402


@pytest.fixture(scope="module")
def client():
    try:
        with TestClient(webapp.app) as c:
            yield c
    except Exception as e:           # no database on this machine
        pytest.skip(f"web app could not start: {e}")


def test_pages_load(client):
    for path in ("/", "/about"):
        r = client.get(webapp.ROOT + path)
        assert r.status_code == 200
        assert "Content-Security-Policy" in r.headers
    assert 'name="question"' in client.get(webapp.ROOT + "/").text


def test_empty_and_long_questions_are_refused(client):
    r = client.post(webapp.ROOT + "/ask", data={"question": "   "})
    assert r.status_code == 400 and "Type a question first" in r.text
    r = client.post(webapp.ROOT + "/ask", data={"question": "x" * 1000})
    assert r.status_code == 400 and "too long" in r.text


def test_own_key_needs_both_service_and_key(client):
    r = client.post(webapp.ROOT + "/ask", data={"question": "How many people?", "provider": "anthropic"})
    assert r.status_code == 400 and "paste the key" in r.text


def test_unknown_answer_is_404(client):
    assert client.get(webapp.ROOT + "/a/doesnotexist").status_code == 404
    assert client.get(webapp.ROOT + "/a/doesnotexist/table/1.xlsx").status_code == 404


def test_model_text_cannot_inject_html():
    out = str(webapp._markdown('Hello <script>alert(1)</script> **bold** [x](javascript:alert(1))'))
    assert "<script>" not in out and "<strong>bold</strong>" in out
    assert 'href="javascript' not in out


def test_number_formatting():
    assert webapp._num(35199.0) == "35,199"
    assert webapp._num(19.81) == "19.81"
    assert webapp._num(None) == ""
    assert webapp._num("2022") == "2022"


TABLE = {"title": "Daonra: Dún Laoghaire agus Gaillimh, 2022", "headings": ["Áit", "Daonra"],
         "columns": ["county", "value"], "rows": [["Dún Laoghaire", 233860.0], ["Gaillimh", 277737.0]],
         "sql": "SELECT 1"}
ROW = {"question": "Cé mhéad duine?", "sources": [
    {"title": "Population", "code": "F1001", "period": "2022", "updated": "2023-06-29",
     "link": "https://data.cso.ie/table/F1001"}]}


def test_excel_file_has_numbers_and_a_source_sheet():
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(files.xlsx(ROW, TABLE, "http://example/a/1")))
    assert wb.sheetnames == ["Table", "Source"]
    ws = wb["Table"]
    assert ws["A2"].value == "Dún Laoghaire" and ws["B2"].value == 233860
    assert isinstance(ws["B2"].value, int) and ws.freeze_panes == "A2"
    source = " ".join(str(c.value) for row in wb["Source"].iter_rows() for c in row)
    assert "F1001" in source and "http://example/a/1" in source and "SELECT 1" in source


def test_csv_opens_in_excel_with_fadas():
    data = files.csv_bytes(TABLE)
    assert data.startswith(b"\xef\xbb\xbf")
    assert "Dún Laoghaire,233860" in data.decode("utf-8")


def test_bar_chart_comes_from_the_rows():
    from dgi_explore.web import chart
    spec = chart.spec(dict(TABLE, chart="bar"))
    assert spec["kind"] == "bar" and spec["measure"] == "Daonra"
    assert [b["label"] for b in spec["bars"]] == ["Dún Laoghaire", "Gaillimh"]
    assert spec["bars"][1]["w"] == 100 and spec["bars"][1]["text"] == "277,737"
    assert 84 < spec["bars"][0]["w"] < 85          # 233,860 of 277,737, from zero
    assert chart.spec(TABLE) is None                # no chart unless one was asked for
    assert chart.spec(dict(TABLE, chart="line")) is None   # two rows are not a trend


def test_line_chart_scale_and_labels():
    from dgi_explore.web import chart
    rows = [[str(y), 100.0 + i * 10, None if i == 2 else 50.0 - i] for i, y in enumerate(range(2016, 2023))]
    spec = chart.spec({"title": "x", "headings": ["Year", "A", "B"], "rows": rows, "chart": "line"})
    assert [s["name"] for s in spec["series"]] == ["A", "B"]
    assert len(spec["series"][0]["segs"]) == 6 and len(spec["series"][1]["segs"]) == 4   # gap at the missing value
    assert spec["ticks"][0]["text"] == "0" and spec["xlabels"][0]["text"] == "2016"
    assert [x["text"] for x in spec["xlabels"]] == ["2016", "2018", "2020", "2022"]
    assert all(chart.TOP <= p["y"] <= chart.BOTTOM for s in spec["series"] for p in s["points"])
    assert chart._label(2022.0) == "2022"
    only_b = chart.spec({"title": "x", "headings": ["Year", "A", "B"], "rows": rows, "chart": "line",
                         "chart_columns": ["B", "no such heading"]})
    assert [s["name"] for s in only_b["series"]] == ["B"] and only_b["measure"] == "B"


def test_line_chart_runs_from_the_first_date_on_the_left():
    from dgi_explore.web import chart
    rows = [["2026", 1039.9], ["2025", 1039.9], ["2024", 1077.3], ["2023", 1105.3]]   # ORDER BY year DESC
    spec = chart.spec({"title": "x", "headings": ["Year", "Cattle"], "rows": rows, "chart": "line"})
    assert spec["xlabels"][0]["text"] == "2023" and spec["xlabels"][-1]["text"] == "2026"
    rows12 = [[str(y), float(y)] for y in range(2026, 2014, -1)]
    labels = [x["text"] for x in chart.spec({"title": "x", "headings": ["Year", "A"], "rows": rows12,
                                             "chart": "line"})["xlabels"]]
    assert labels == ["2016", "2018", "2020", "2022", "2024", "2026"]
    assert spec["series"][0]["end"]["text"] == "1,039.9"
    k = chart._time_key
    assert k(2022.0) < k("2022M03") < k("2022Q2") < k("2022 October") < k("2022-12-31") < k("2023")
    assert k("March 2022") == k("2022M03") and k("2021/2022") < k("2022/2023")
    assert k("Co. Cork") is None and k("Dublin 2022 electoral area") is None
    months = ["2024M12", "2024M02", "2025M01"]
    assert [r[0] for r in chart._in_time_order([[m, 1] for m in months])] == ["2024M02", "2024M12", "2025M01"]
    names = [["Cork", 1], ["Antrim", 2], ["Kerry", 3]]
    assert chart._in_time_order(names) == names     # not times: left as given


def test_follow_up_carries_the_earlier_turns():
    from dgi_explore.ask import agent
    text = agent._with_context("And for Mayo?", [
        {"question": "How many people live in Leitrim?", "answer": "35,199 in 2022.",
         "tables": ["Population, 2022"], "queries": ["SELECT 1"]}])
    assert "How many people live in Leitrim?" in text and "SELECT 1" in text
    assert text.rstrip().endswith("And for Mayo?")


def test_follow_up_of_an_unknown_answer_is_an_ordinary_question(client):
    r = client.post(webapp.ROOT + "/ask", data={"question": " ", "after": "doesnotexist"})
    assert r.status_code == 400 and 'name="after"' not in r.text


def test_source_lines_written_by_the_model_are_dropped():
    from dgi_explore.ask import agent
    assert agent._without_source_lines("Mayo has most.\n\nSource: CSO table F1020.") == "Mayo has most."
    assert agent._without_source_lines("**Sources:** CSO tables A and B.") == ""
    assert agent._without_source_lines("The source of the Shannon is in Cavan.") == "The source of the Shannon is in Cavan."


def test_postgres_numeric_stays_a_number():
    from decimal import Decimal
    from dgi_explore.ask import tools
    assert tools._plain(Decimal("19.8")) == 19.8 and isinstance(tools._plain(Decimal("19.8")), float)
    assert tools._plain(Decimal("137970")) == 137970 and isinstance(tools._plain(Decimal("137970")), int)
    assert tools._plain(None) is None and tools._plain("x") == "x"


def _answer(aid, question, parent=None, status="done", feedback=None):
    import datetime
    return {"id": aid, "question": question, "parent": parent, "status": status,
            "answer_md": f"Answer to {question}", "tables": [dict(TABLE, chart="bar")],
            "sources": ROW["sources"], "steps": [{"say": "Looked", "sql": "SELECT 1"}],
            "model": "m", "seconds": 3.0, "own_key": False, "feedback": feedback, "error": None,
            "finished_at": datetime.datetime(2026, 11, 20)}


def test_a_follow_up_page_shows_the_whole_conversation(client, monkeypatch):
    rows = {"a1": _answer("a1", "First?"), "b-2": _answer("b-2", "Second?", "a1", feedback=1),
            "c_3": _answer("c_3", "Third?", "b-2")}
    monkeypatch.setattr(webapp.store, "get", rows.get)
    monkeypatch.setattr(webapp.store, "thread", lambda aid, limit=6: (
        [rows["a1"]] if aid == "a1" else [rows["a1"], rows["b-2"]]))
    page = client.get(webapp.ROOT + "/a/c_3").text
    page = page[page.index("<main"):]                 # not the <title>
    # every turn in full, oldest first, the first question as the page's h1
    assert page.index("First?") < page.index("Second?") < page.index("Third?")
    assert page.count("<svg") >= 3 and page.count("Dún Laoghaire") >= 3
    assert '<h1 class="question">First?</h1>' in page and '<h2 class="question">Third?</h2>' in page
    assert page.count("<h1") == 1 and 'id="t-c_3"' in page
    # each turn's ids are its own, and a reply comes back to this page
    assert page.count('id="answer-a1"') == 1 and page.count('id="answer-c_3"') == 1
    assert 'name="back" value="c_3"' in page and page.count("Thank you") == 1
    # the follow-up form follows on from the latest answer
    assert 'name="after" value="c_3"' in page

    # a follow-up still being worked on shows the conversation above its steps
    rows["c_3"] = dict(rows["c_3"], status="working")
    page = client.get(webapp.ROOT + "/a/c_3").text
    page = page[page.index("<main"):]
    assert page.index("Second?") < page.index('id="steps"') and "Answer to Second?" in page

    # feedback on an earlier turn returns to the page it was given on
    monkeypatch.setattr(webapp.store, "set_feedback", lambda aid, vote: None)
    r = client.post(webapp.ROOT + "/a/a1/feedback", data={"vote": "yes", "back": "c_3"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("/a/c_3#feedback-a1")
    r = client.post(webapp.ROOT + "/a/a1/feedback", data={"vote": "yes", "back": "https://evil"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("/a/a1#feedback-a1")


def test_a_question_needs_the_solved_puzzle(client, monkeypatch):
    import hashlib
    from dgi_explore.web import proof
    monkeypatch.setattr(proof, "MAX_NUMBER", 500)
    p = proof.challenge()
    n = next(i for i in range(501) if hashlib.sha256(f"{p['salt']}{i}".encode()).hexdigest() == p["challenge"])
    args = (p["salt"], p["challenge"], p["signature"])
    assert not proof.check(*args, n + 1 if n < 500 else n - 1)          # wrong number
    assert not proof.check(p["salt"], p["challenge"], "0" * 64, n)      # not signed by the site
    assert proof.check(*args, n)                                         # solved
    assert not proof.check(*args, n)                                     # and only once
    assert not proof.check("1.abc", p["challenge"], p["signature"], n)  # expired / altered
    # the form page carries a puzzle and the script; a question without the answer is refused
    page = client.get(webapp.ROOT + "/").text
    assert 'name="proof"' in page and "proof.js" in page
    r = client.post(webapp.ROOT + "/ask", data={"question": "How many people live in Leitrim?"})
    assert r.status_code == 403 and "quick check" in r.text


def test_search_engines_are_kept_out(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Disallow: /" in r.text
    assert "noindex" in client.get(webapp.ROOT + "/").headers["X-Robots-Tag"]


def test_tables_the_cso_only_publishes_name_their_owner(client):
    from dgi_explore.web import store
    if store._read_owners is None or not store.owner("NRA01"):
        pytest.skip("no table index on this machine")
    assert store.owner("NRA01") == store.owner("nra01") == "Transport Infrastructure Ireland"
    assert store.owner("F1001") is None and store.owner(None) is None


# Security review 20261120_ask-security-review.org, findings 1, 2, 3, 4 and 8.

def test_an_ipv6_block_counts_as_one_visitor():
    from dgi_explore.web import limits, store
    assert limits.network("2a01:4f8:13b:155f::2") == limits.network("2a01:4f8:13b:155f:ffff::9") \
        == "2a01:4f8:13b:155f::/64"
    assert limits.network("2a01:4f8:13b:1560::2") != limits.network("2a01:4f8:13b:155f::2")
    assert limits.network("::ffff:203.0.113.5") == limits.network("203.0.113.5") == "203.0.113.5"
    assert store.client_hash("2a01:db8:1:2::1") == store.client_hash("2a01:db8:1:2:aaaa::7")
    assert store.client_hash("203.0.113.5") != store.client_hash("203.0.113.6")


def test_questions_per_minute_and_open_connections_are_limited():
    from dgi_explore.web import limits
    k = "test-visitor"
    assert all(limits.may_ask(k, now=1000 + i) for i in range(limits.ASK_PER_MINUTE))
    assert not limits.may_ask(k, now=1010)
    assert limits.may_ask(k, now=1061)                # a minute later
    taken = [limits.open_events("v2") for _ in range(limits.EVENTS_PER_VISITOR + 1)]
    assert taken == [True] * limits.EVENTS_PER_VISITOR + [False]
    for _ in range(limits.EVENTS_PER_VISITOR):
        limits.close_events("v2")
    assert limits.open_events("v2")
    limits.close_events("v2")


def test_live_steps_stream_and_its_limit(client):
    from dgi_explore.web import limits, runner
    j = runner.Job()
    j.say("Looking")
    j.finish()
    runner._jobs["streamtest"] = j
    try:
        r = client.get(webapp.ROOT + "/a/streamtest/events", headers={"x-real-ip": "198.51.100.7"})
        assert r.status_code == 200 and "Looking" in r.text and "event: done" in r.text
        key = limits.visitor("198.51.100.7")
        for _ in range(limits.EVENTS_PER_VISITOR):
            limits.open_events(key)
        r = client.get(webapp.ROOT + "/a/streamtest/events", headers={"x-real-ip": "198.51.100.7"})
        assert r.status_code == 429
        for _ in range(limits.EVENTS_PER_VISITOR):
            limits.close_events(key)
    finally:
        runner._jobs.pop("streamtest", None)


def test_downloads_never_contain_live_formulas():
    from openpyxl import load_workbook
    bad = '=HYPERLINK("https://example.com","click")'
    table = dict(TABLE, title="=1+1", headings=["@SUM(A1)", "Daonra"], rows=[[bad, 5.0], ["-x", 1.0]])
    wb = load_workbook(io.BytesIO(files.xlsx(dict(ROW, question="+cmd"), table, "http://e/a/1")))
    cells = [c for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    assert not [c for c in cells if c.data_type == "f"]
    assert wb["Table"]["A2"].value == bad and wb["Table"]["B2"].value == 5
    text = files.csv_bytes(table).decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'@SUM" in text and ",5\r\n" in text


def test_answers_link_only_to_the_cso():
    html = webapp._markdown("[CSO](https://data.cso.ie/table/F1001) [bad](https://evil.example/x) "
                            "<https://evil.example> [js](javascript:alert(1))")
    assert 'href="https://data.cso.ie/table/F1001"' in html
    assert html.count("<a ") == 1 and "evil.example/x" not in html and "javascript:" not in html.split("[js]")[0]


def test_an_own_key_error_message_is_not_kept(monkeypatch):
    from dgi_explore.web import runner
    saved = {}
    monkeypatch.setattr(runner.agent, "answer", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("model service error 401: Incorrect API key provided: sk-proj-abcd****wxyz")))
    monkeypatch.setattr(runner.store, "fail", lambda aid, err, steps: saved.update(err=err))
    runner._jobs["ownkey"] = runner.Job()
    runner._run("ownkey", "q", "openai", "m", None, "sk-proj-secret", None)
    assert saved["err"] == "RuntimeError" and "sk-" not in saved["err"]
    runner._jobs["sitekey"] = runner.Job()
    runner._run("sitekey", "q", "deepseek", "m", None, None, None)
    assert "401" in saved["err"]                      # the site's own errors keep their detail

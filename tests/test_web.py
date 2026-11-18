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

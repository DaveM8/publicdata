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

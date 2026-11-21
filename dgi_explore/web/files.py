"""Download a table from an answer as a real Excel file, or as CSV.

The Excel file has two sheets: the table (numbers as numbers, header frozen and
filterable) and Source, which says where the figures came from and how they
were worked out, so the provenance travels with the file.
"""

import csv
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from . import store


# Text that a spreadsheet would run as a formula. Headings, titles and text
# columns come from the model's query, so a crafted question could otherwise
# put a live formula in a download (security review finding 3).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _as_text(ws):
    """Every text cell that looks like a formula is stored as plain text."""
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith(FORMULA_START):
                cell.data_type = "s"


def _csv_safe(v):
    return "'" + v if isinstance(v, str) and v.startswith(FORMULA_START) else v


def _width(values):
    return max(8, min(60, max(len(str(v)) for v in values) + 2))


def xlsx(row, table, page_url):
    """row: the app.answer record; table: one entry of its tables list."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Table"
    ws.append(table["headings"])
    for r in table["rows"]:
        ws.append([int(v) if isinstance(v, float) and v.is_integer() else v for v in r])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, heading in enumerate(table["headings"], 1):
        col = [heading] + [r[i - 1] for r in table["rows"] if r[i - 1] is not None]
        ws.column_dimensions[get_column_letter(i)].width = _width(col)

    src = wb.create_sheet("Source")
    lines = [("Table", table["title"]),
             ("Question asked", row["question"]),
             ("Answer page", page_url),
             ("Downloaded", date.today().isoformat())]
    for s in row["sources"]:
        who = store.owner(s["code"]) or "Central Statistics Office"
        lines += [("Source", f'{who}, {s["title"]} ({s["code"]}), published on data.cso.ie'),
                  ("  Period covered", s["period"]),
                  ("  Last updated by the CSO", s["updated"]),
                  ("  Link", s["link"])]
    lines += [("How it was worked out", "The figures are the result of this query on a copy of "
               "the CSO's published tables taken in October 2026:"),
              ("", table["sql"])]
    for a, b in lines:
        src.append([a, b])
    for cell in src["A"]:
        cell.font = Font(bold=True)
    for cell in src["B"]:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    src.column_dimensions["A"].width = 28
    src.column_dimensions["B"].width = 100
    _as_text(ws)
    _as_text(src)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def csv_bytes(table):
    """CSV with a byte-order mark, so that Excel shows the fadas correctly."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([_csv_safe(h) for h in table["headings"]])
    for r in table["rows"]:
        w.writerow([int(v) if isinstance(v, float) and v.is_integer() else _csv_safe(v) for v in r])
    return ("﻿" + out.getvalue()).encode("utf-8")


def filename(table, ext):
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", table["title"]).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:60] or "table"
    return f"{s}.{ext}"

"""A chart for a table on an answer page, worked out on the server.

The model only says which kind it wants ("bar" or "line") when it calls
show_table. Everything drawn comes from the table's own rows, like the table
itself. spec() turns a table into positions in percent, and the template
templates/_chart.html draws them as plain SVG: no JavaScript, nothing loaded
from anywhere, and it prints. Returns None when the rows do not suit a chart,
in which case the page shows the table alone.
"""

import math
import re

MAX_BARS = 35           # room for the 31 local authorities
MAX_POINTS = 80
MAX_SERIES = 4
MARKERS_UP_TO = 30       # points per line that still get a dot each
TOP, BOTTOM = 10.0, 86.0  # the plot's top and bottom edge, percent of the SVG's height
LEFT, RIGHT = 1.5, 98.5


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _numeric(table, i):
    vals = [r[i] for r in table["rows"] if r[i] is not None]
    return bool(vals) and all(_is_num(v) for v in vals)


def fmt(v):
    """A figure as people write it: 1,234 or 19.8."""
    if v is None:
        return ""
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _axis_text(v):
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:g} billion"
    if a >= 1e6:
        return f"{v / 1e6:g} million"
    return fmt(v)


def _label(v):
    if _is_num(v) and float(v).is_integer():
        return str(int(v))          # a year held as a number: 2022, not 2,022
    return "" if v is None else str(v)


MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _time_key(v):
    """Where a time label falls, for putting a line chart's points in time order:
    2022, "2022", "2022M03", "2022Q1", "2022W05", "2022-03-31", "2022 March",
    "March 2022", "2021/2022". None when the label isn't a time."""
    if _is_num(v) and float(v).is_integer() and 1800 <= v <= 2200:
        return (int(v), 0, 0)
    s = str(v).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s)
    if m:
        return (int(m[1]), int(m[2]), int(m[3] or 0))
    m = re.fullmatch(r"(\d{4})\s*([MQW])\s*(\d{1,2})", s, re.I)
    if m:
        k = int(m[3])
        return {"M": (int(m[1]), k, 0), "Q": (int(m[1]), k * 3, 0),
                "W": (int(m[1]), 0, k)}[m[2].upper()]
    m = re.search(r"\b(1[89]\d\d|2[01]\d\d)\b", s)
    if not m:
        return None
    rest = (s[:m.start()] + " " + s[m.end():]).lower()
    words = re.findall(r"[a-z]+", rest)
    if len(words) > 1 or (words and words[0][:3] not in MONTHS) \
            or re.sub(r"[a-z]+|[\s/–-]|\d{2,4}", "", rest):
        return None              # more than a year and a month: not a time label
    return (int(m[1]), MONTHS[words[0][:3]] if words else 0, 0)


def _in_time_order(rows):
    """Rows oldest first when the first column is a time; otherwise as given.
    A query often lists the newest first, but a line chart reads left to right."""
    keys = [_time_key(r[0]) for r in rows]
    if None in keys:
        return rows
    return [r for _, r in sorted(zip(keys, rows), key=lambda kr: kr[0])]


def _ticks(lo, hi, about=4):
    """Round numbers covering lo..hi."""
    raw = (hi - lo) / about
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    out = [first]
    while out[-1] < hi - step * 1e-9:
        out.append(first + step * len(out))
    return [round(x, 10) for x in out]


def _wanted(table, candidates):
    """The figure columns to draw: the ones named in chart_columns, else all of them."""
    named = [table["headings"].index(h) for h in table.get("chart_columns") or []
             if h in table["headings"]]
    return [i for i in named if i in candidates] or candidates


def _bar(table):
    numeric = [_numeric(table, i) for i in range(len(table["headings"]))]
    names = [i for i, n in enumerate(numeric) if not n] or [0]
    values = [i for i, n in enumerate(numeric) if n and i not in names]
    if not values:
        return None
    vc = _wanted(table, values)[0]
    rows = [r for r in table["rows"] if r[vc] is not None]
    if not 2 <= len(rows) <= MAX_BARS:
        return None
    lo = min(0.0, min(r[vc] for r in rows))
    hi = max(0.0, max(r[vc] for r in rows))
    if hi == lo:
        return None
    span = hi - lo
    bars = [{"label": ", ".join(_label(r[i]) for i in names),
             "text": fmt(r[vc]),
             "x": round((min(r[vc], 0) - lo) / span * 100, 2),
             "w": round(abs(r[vc]) / span * 100, 2)} for r in rows]
    return {"kind": "bar", "measure": table["headings"][vc], "bars": bars,
            "zero": round(-lo / span * 100, 2) if lo < 0 else None}


def _line(table):
    rows = _in_time_order(table["rows"])
    cols = _wanted(table, [i for i in range(1, len(table["headings"]))
                           if _numeric(table, i)])[:MAX_SERIES]
    if not cols or not 3 <= len(rows) <= MAX_POINTS:
        return None
    vals = [r[i] for r in rows for i in cols if r[i] is not None]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return None
    if 0 <= lo <= 0.6 * hi:
        lo = 0.0                     # start at zero unless that would flatten the line
    ticks = _ticks(lo, hi)
    d0, d1 = ticks[0], ticks[-1]
    n = len(rows)
    xs = [round(LEFT + i / (n - 1) * (RIGHT - LEFT), 2) for i in range(n)]
    y = lambda v: round(TOP + (1 - (v - d0) / (d1 - d0)) * (BOTTOM - TOP), 2)

    series = []
    for k, c in enumerate(cols, 1):
        pts = [{"x": xs[i], "y": y(r[c]), "tip": f"{_label(r[0])}: {fmt(r[c])}"}
               if r[c] is not None else None for i, r in enumerate(rows)]
        segs = [{"x1": a["x"], "y1": a["y"], "x2": b["x"], "y2": b["y"]}
                for a, b in zip(pts, pts[1:]) if a and b]
        real = [p for p in pts if p]
        last = next((r[c] for r in reversed(rows) if r[c] is not None), None)
        series.append({"n": k, "name": table["headings"][c], "segs": segs,
                       "points": real if n <= MARKERS_UP_TO else real[-1:],
                       "end": dict(real[-1], text=fmt(last)) if real else None})

    labels = [_label(r[0]) for r in rows]
    want = 4 if max(len(s) for s in labels) > 7 else 6
    every = math.ceil(n / want)          # label every so many points, evenly, back from the last,
    xlabels = [{"x": xs[i], "text": labels[i],   # so the latest date is always labelled
                "anchor": "start" if i == 0 else "end" if i == n - 1 else "middle"}
               for i in sorted(range(n - 1, -1, -every))]
    tick_rows = [{"y": y(v), "text": _axis_text(v)} for v in ticks]
    return {"kind": "line", "series": series, "xlabels": xlabels, "ticks": tick_rows,
            "axis_em": round(0.58 * max(len(tk["text"]) for tk in tick_rows) + 0.8, 1),
            "measure": series[0]["name"] if len(series) == 1 else None}


def spec(table):
    """What to draw for this table, or None for no chart."""
    try:
        kind = table.get("chart")
        if kind == "bar":
            return _bar(table)
        if kind == "line":
            return _line(table)
    except Exception:        # an odd table must never break the answer page
        return None
    return None

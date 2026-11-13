"""Phase 2: profile every downloaded file into dgi_prof.

For each file (once per sha256): its real type from the first bytes, its text
encoding, and a kind-specific profile:

- table     CSV/TSV and spreadsheet sheets: columns, types, distinct counts, top values
- jsonstat  CSO cubes: dimensions with roles (geo, time, metric), categories, units, notes
- geo       every vector layer, also inside zips: fields, geometry type, feature count,
            coordinate system, extent and a few sample rows
- raster    GeoTIFF etc.: size, coordinate system, extent
- document  PDF, Word, plain text: the readable text
- webpage   HTML (often a landing or error page posing as data): the readable text
- archive   zips: their members, each profiled as above

All text goes through textio, so Irish characters come out right, and every
repaired value is logged to dgi_prof.text_fix. Runs in parallel; results are
written by the parent process only. Already-profiled files (same sha256 and
VERSION) are skipped, so an interrupted run just carries on.
"""

import csv
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from collections import Counter
from multiprocessing import Pool
from pathlib import Path, PurePosixPath

from . import db, textio

# Some CSV cells hold whole documents or geometries.
csv.field_size_limit(1 << 30)

log = logging.getLogger("dgi")
# openpyxl warns about every workbook without a default style; it's harmless.
warnings.filterwarnings("ignore", module="openpyxl")

VERSION = 1
TMP_ROOT = Path("/mnt/pdf_workspace/dgx-tmp")   # NVMe scratch, for zip members
ROW_SAMPLE = 100_000        # rows examined per table for types and value counts
DISTINCT_CAP = 20_000
MAX_DOC_CHARS = 200_000
MAX_ZIP_MEMBERS = 300
MAX_CATEGORIES = 25000       # CSO small-area tables have ~18,900 categories
GDAL_TIMEOUT = 300

GEO_EXT = {".shp", ".geojson", ".gpkg", ".kml", ".kmz", ".gml", ".tab", ".mif", ".gdb", ".sqlite", ".fgb"}
TABLE_EXT = {".csv", ".tsv", ".txt", ".tab", ".psv"}
SHEET_EXT = {".xlsx", ".xlsm", ".xls", ".ods"}
DOC_EXT = {".pdf", ".docx", ".html", ".htm", ".xml"}
RASTER_EXT = {".tif", ".tiff", ".asc", ".img", ".jp2", ".ecw"}


# ── results ──────────────────────────────────────────────────────────────────

class Result:
    """Everything learned about one file; plain data so it can cross processes."""

    def __init__(self, sha256, path, size):
        self.file = {"sha256": sha256, "path": path, "bytes": size, "summary": {}}
        self.cols, self.layers, self.fixes = [], [], []
        self.jsonstat = None
        self.doc = None

    def fix(self, field, before, after):
        if len(self.fixes) < 500:
            self.fixes.append((self.file["path"], str(field), before, after))


# ── value typing for table columns ───────────────────────────────────────────

_INT = re.compile(r"^[-+]?\d{1,3}(,\d{3})+$|^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d*)\.\d+([eE][-+]?\d+)?$|^[-+]?\d+[eE][-+]?\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+Z?)?$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
_YEAR = re.compile(r"^(1[89]|20|21)\d{2}$")


def value_type(v):
    v = v.strip()
    if not v:
        return "empty"
    if _YEAR.match(v):
        return "year"
    if _INT.match(v):
        return "int"
    if _FLOAT.match(v):
        return "float"
    if _DATE.match(v):
        return "date"
    return "text"


class ColumnStats:
    def __init__(self, name):
        self.name = name
        self.types = Counter()
        self.values = Counter()
        self.capped = False
        self.n = 0

    def add(self, v):
        self.n += 1
        self.types[value_type(v)] += 1
        v = v.strip()
        if v and (v in self.values or len(self.values) < DISTINCT_CAP):
            self.values[v] += 1
        elif v:
            self.capped = True

    def row(self, part, ordinal):
        filled = self.n - self.types["empty"]
        t = "empty"
        if filled:
            kinds = {k: c for k, c in self.types.items() if k != "empty"}
            top, cnt = max(kinds.items(), key=lambda kv: kv[1])
            if cnt >= 0.95 * filled:
                t = top
            elif set(kinds) <= {"int", "float", "year"}:
                t = "float" if "float" in kinds else "int"
            else:
                t = "mixed"
        return {
            "part": part, "ordinal": ordinal, "name": self.name, "role": None,
            "inferred_type": t,
            "null_frac": round(self.types["empty"] / self.n, 4) if self.n else None,
            "n_distinct": len(self.values), "distinct_capped": self.capped,
            "top_values": self.values.most_common(10),
            "samples": [v for v, _ in self.values.most_common(5)],
            # Kept for recognising location/time columns (Phase 3); not for measures.
            "distinct_values": (sorted(self.values) if t in ("text", "mixed", "int", "year", "date")
                                and not self.capped else None),
        }


def find_header(head):
    """Index of the header row among the first rows: the first with the most
    common number of fields (skips title and note lines above a table)."""
    widths = Counter(len(r) for r in head if any(c.strip() for c in r))
    if not widths:
        return None, 0
    width = widths.most_common(1)[0][0]
    return next(i for i, r in enumerate(head) if len(r) == width), width


def profile_rows(rows, part, res, max_header_scan=15):
    """Profile an iterator of rows (lists of strings). Returns (n_rows, n_cols).

    Finds the header: the first row, within the first 15, that has the most
    common number of fields (skips title and note lines above a table).
    """
    head = []
    for r in rows:
        head.append(r)
        if len(head) >= max_header_scan:
            break
    if not head:
        return 0, 0
    widths = Counter(len(r) for r in head if any(c.strip() for c in r))
    if not widths:
        return 0, 0
    width = widths.most_common(1)[0][0]
    hi = next(i for i, r in enumerate(head) if len(r) == width)
    header = [h.strip() or f"column_{i + 1}" for i, h in enumerate(head[hi])]
    stats = [ColumnStats(h) for h in header]
    n = 0

    def take(r):
        nonlocal n
        n += 1
        if n <= ROW_SAMPLE:
            for i, s in enumerate(stats):
                s.add(r[i] if i < len(r) else "")

    for r in head[hi + 1:]:
        take(r)
    for r in rows:
        take(r)
    for i, s in enumerate(stats):
        res.cols.append(s.row(part, i))
    return n, len(header)


# ── kinds ────────────────────────────────────────────────────────────────────

def profile_text_table(path, res, part=""):
    """CSV, TSV, or plain text that may be a table."""
    enc = textio.detect_encoding(path)
    f = res.file
    if not part:
        f.update(encoding=enc.encoding, had_bom=enc.had_bom, mixed=enc.encoding == "mixed")
    fixes = []
    sample = "".join(line for _, line in zip(range(200), textio.iter_lines(path, enc)))
    try:
        dialect = csv.Sniffer().sniff(sample[:65536], delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = None
    if delim is None:
        if "," in sample and sample.count("\n") > 1:
            delim = ","
        else:
            return profile_document_text(path, res, enc, part)
    rows = csv.reader(textio.iter_lines(path, enc, fixes), delimiter=delim)
    try:
        n_rows, n_cols = profile_rows(rows, part, res)
    except csv.Error as e:
        res.file["summary"].setdefault("warnings", []).append(f"{part or 'file'}: csv error {e}")
        n_rows, n_cols = 0, 0
    for line, before, after in fixes:
        res.fix(f"{part}:line {line}" if part else f"line {line}", before, after)
    if not part:
        f.update(kind="table", n_rows=n_rows, n_cols=n_cols, fixed_count=len(fixes))
        f["summary"]["delimiter"] = delim
    return n_rows


def profile_document_text(path, res, enc, part=""):
    t = textio.read_text(path)
    for before, after in t.fixes:
        res.fix(part or "text", before, after)
    if not part:
        res.file.update(kind="document", fixed_count=len(t.fixes), nfc_changed=t.nfc_changed)
    add_doc(res, None, t.text, part)


def add_doc(res, title, text, part=""):
    text = (text or "").strip()
    if not text:
        return
    if res.doc is None:
        res.doc = {"title": title, "text": "", "chars": 0}
    if part:
        text = f"[{part}]\n{text}"
    res.doc["chars"] += len(text)
    room = MAX_DOC_CHARS - len(res.doc["text"])
    if room > 0:
        res.doc["text"] += ("\n\n" if res.doc["text"] else "") + text[:room]
    res.doc["title"] = res.doc["title"] or title


def profile_json(path, res):
    t = textio.read_text(path)
    for before, after in t.fixes:
        res.fix("json", before, after)
    res.file.update(encoding=t.encoding.encoding, had_bom=t.encoding.had_bom,
                    fixed_count=len(t.fixes), nfc_changed=t.nfc_changed)
    try:
        doc = json.loads(t.text)
    except json.JSONDecodeError as e:
        res.file.update(kind="json", error=f"invalid JSON: {e}")
        return
    if isinstance(doc, dict) and (doc.get("class") == "dataset" or "dataset" in doc and "dimension" in str(doc)[:5000]):
        return profile_jsonstat(doc, res)
    if isinstance(doc, dict) and doc.get("type") in ("FeatureCollection", "Feature"):
        res.file["kind"] = "geo"
        return profile_geo(str(db.MIRROR / res.file["path"]), res, "")
    res.file["kind"] = "json"
    res.file["summary"]["top_keys"] = list(doc)[:50] if isinstance(doc, dict) else None
    res.file["summary"]["list_length"] = len(doc) if isinstance(doc, list) else None
    add_doc(res, None, json.dumps(doc, ensure_ascii=False)[:20000])


def profile_jsonstat(doc, res):
    """JSON-stat 2.0 (class: dataset) or 1.0 ({"dataset": {...}})."""
    version = "2.0"
    if doc.get("class") != "dataset":
        version = "1.0"
        doc = doc.get("dataset") or next(iter(doc.values()))
    dim = doc.get("dimension", {})
    ids = doc.get("id") or dim.get("id") or []
    sizes = doc.get("size") or dim.get("size") or []
    role = doc.get("role") or dim.get("role") or {}
    role_of = {d: r for r, ds in role.items() for d in ds}
    dims, units = [], {}
    for i, d in enumerate(ids):
        dd = dim.get(d, {})
        cat = dd.get("category", {})
        index = cat.get("index")
        codes = (list(index) if isinstance(index, list) else
                 [c for c, _ in sorted(index.items(), key=lambda kv: kv[1])] if isinstance(index, dict) else
                 list(cat.get("label", {})))
        labels = cat.get("label", {})
        dims.append({"id": d, "label": dd.get("label"), "role": role_of.get(d),
                     "size": sizes[i] if i < len(sizes) else len(codes),
                     "categories": {c: labels.get(c, c) for c in codes[:MAX_CATEGORIES]}})
        if cat.get("unit"):
            units.update(cat["unit"])
        res.cols.append({
            "part": "", "ordinal": i, "name": dd.get("label") or d, "role": role_of.get(d),
            "inferred_type": "dimension", "null_frac": None, "n_distinct": len(codes),
            "distinct_capped": False, "top_values": None,
            "samples": [labels.get(c, c) for c in codes[:5]], "distinct_values": None})
    values = doc.get("value")
    n_values = len(values) if values is not None else 0
    ext = doc.get("extension") or {}
    notes = doc.get("note") or []
    res.jsonstat = {"matrix": ext.get("matrix") or (doc.get("label") or "").split(" - ")[0][:40],
                    "label": doc.get("label"), "updated": doc.get("updated"), "version": version,
                    "n_values": n_values, "dims": dims, "units": units,
                    "notes": notes if isinstance(notes, list) else [str(notes)], "extension": ext}
    res.file.update(kind="jsonstat", n_rows=n_values, n_cols=len(ids))


def _ogr_json(args, env=None):
    p = subprocess.run(["ogrinfo", "-json", "-ro", "-so", *args], capture_output=True,
                       timeout=GDAL_TIMEOUT, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[:500])
    return json.loads(p.stdout.decode("utf-8", "replace"))


def _ogr_sample(gdal_path, layer, enc_args, res, field_part, n=3):
    p = subprocess.run(["ogr2ogr", "-f", "CSV", "/vsistdout/", *enc_args, "-limit", str(n),
                        gdal_path, layer], capture_output=True, timeout=GDAL_TIMEOUT)
    if p.returncode != 0:
        return []
    rows = list(csv.DictReader(p.stdout.decode("utf-8", "replace").splitlines()))
    out = []
    for r in rows:
        clean_row = {}
        for k, v in r.items():
            if v and textio.MOJIBAKE.search(v):
                fixed = textio.clean(v)
                if fixed != v:
                    res.fix(f"{field_part}:{layer}.{k}", v, fixed)
                v = fixed
            clean_row[textio.clean(k or "")] = v
        out.append(clean_row)
    return out


def _shapefile_enc(shp_path, zf=None, member=None):
    """SHAPE_ENCODING for a .shp on disk or inside a zip."""
    if zf is not None:
        base = member[:-4]
        names = {n.lower(): n for n in zf.namelist()}
        dbf_name = names.get((base + ".dbf").lower())
        cpg_name = names.get((base + ".cpg").lower())
        if not dbf_name:
            return None, "no .dbf"
        with zf.open(dbf_name) as f:
            dbf = f.read(4 << 20)
        cpg = zf.read(cpg_name).decode("ascii", "replace") if cpg_name else None
    else:
        base = shp_path[:-4]
        dbf_path = next((base + e for e in (".dbf", ".DBF") if os.path.exists(base + e)), None)
        if not dbf_path:
            return None, "no .dbf"
        with open(dbf_path, "rb") as f:
            dbf = f.read(4 << 20)
        cpg_path = next((base + e for e in (".cpg", ".CPG") if os.path.exists(base + e)), None)
        cpg = Path(cpg_path).read_text("ascii", "replace") if cpg_path else None
    return textio.shapefile_encoding(dbf, cpg)


def profile_geo(gdal_path, res, part, shape_enc=None):
    """Every layer of a vector dataset GDAL can open."""
    enc_args = ["--config", "SHAPE_ENCODING", shape_enc[0]] if shape_enc else []
    info = _ogr_json([*enc_args, gdal_path])
    for lay in info.get("layers", []):
        gf = (lay.get("geometryFields") or [{}])[0]
        cs = gf.get("coordinateSystem") or {}
        pj = cs.get("projjson") or {}
        pid = pj.get("id") or {}
        srid = pid.get("code") if pid.get("authority") == "EPSG" else None
        if srid is None and pj.get("type") == "GeographicCRS" and "WGS 84" in (pj.get("name") or ""):
            srid = 4326
        name = textio.clean(lay.get("name") or "")
        res.layers.append({
            "part": part, "layer": name, "gdal_path": gdal_path,
            "geom_type": gf.get("type"), "feature_count": lay.get("featureCount"),
            "srid": srid, "srs_name": pj.get("name"), "extent": gf.get("extent"),
            "fields": [[textio.clean(f["name"]), f.get("type")] for f in lay.get("fields", [])],
            "sample": _ogr_sample(gdal_path, lay.get("name"), enc_args, res, part or "layer"),
            "encoding": f"{shape_enc[0]} ({shape_enc[1]})" if shape_enc else None,
        })


def profile_raster(gdal_path, res, part):
    p = subprocess.run(["gdalinfo", "-json", gdal_path], capture_output=True, timeout=GDAL_TIMEOUT)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[:500])
    info = json.loads(p.stdout.decode("utf-8", "replace"))
    pj = (info.get("coordinateSystem") or {}).get("projjson") or {}
    pid = pj.get("id") or {}
    cc = info.get("cornerCoordinates") or {}
    xs = [v[0] for v in cc.values() if isinstance(v, list)]
    ys = [v[1] for v in cc.values() if isinstance(v, list)]
    res.layers.append({
        "part": part, "layer": PurePosixPath(part or res.file["path"]).name, "gdal_path": gdal_path,
        "geom_type": "raster", "feature_count": None,
        "srid": pid.get("code") if pid.get("authority") == "EPSG" else None,
        "srs_name": pj.get("name"),
        "extent": [min(xs), min(ys), max(xs), max(ys)] if xs else None,
        "fields": [["size", f"{info.get('size')}"], ["bands", str(len(info.get('bands', [])))]],
        "sample": None, "encoding": None})


def _rows_openpyxl(ws):
    for row in ws.iter_rows(values_only=True):
        yield [("" if c is None else str(c)) for c in row]


def _rows_xlrd(ws):
    for i in range(ws.nrows):
        yield [("" if c is None else str(c)) for c in ws.row_values(i)]


def profile_sheet_file(path, res, part=""):
    """Spreadsheets. The reader is chosen by what the file really is, not its name:
    a zip is xlsx/xlsm (openpyxl), an OLE file is old .xls (xlrd)."""
    with open(path, "rb") as fh:
        real = textio.sniff_type(fh.read(8))
    sheets = []
    if real == "zip":
        import openpyxl
        # A file object, not the path: openpyxl refuses paths ending in .xls even
        # when the file is really .xlsx, which nine mirror files are.
        wb = openpyxl.load_workbook(open(path, "rb"), read_only=True, data_only=True)
        sheets = [(ws.title, _rows_openpyxl(ws)) for ws in wb.worksheets]
    elif real == "ole":
        import xlrd
        try:
            wb = xlrd.open_workbook(path)
        except xlrd.XLRDError as e:
            if "workbook" in str(e).lower():
                # An old Office file that isn't a spreadsheet: usually a Word .doc.
                if not part:
                    res.file["kind"] = "document"
                    res.file["summary"]["note"] = "old Office file without a workbook (probably .doc); not read"
                return
            raise
        sheets = [(ws.name, _rows_xlrd(ws)) for ws in wb.sheets()]
    else:
        res.file["summary"]["note"] = f"spreadsheet type not profiled ({real})"
        return
    total = 0
    for name, rows in sheets:
        name = textio.clean(name)
        sub = f"{part}#{name}" if part else name

        def cleaned(rows=rows, sub=sub):
            for r in rows:
                out = []
                for v in r:
                    if v and not v.isascii():
                        c = textio.clean(v)
                        if c != v:
                            res.fix(sub, v, c)
                        v = c
                    out.append(v)
                yield out
        total += profile_rows(cleaned(), sub, res)[0]
    res.file["summary"]["sheets"] = [textio.clean(n) for n, _ in sheets]
    if not part:
        res.file.update(kind="table", n_rows=total)


def profile_pdf(path, res, part=""):
    import pymupdf
    with pymupdf.open(path) as doc:
        res.file["summary"]["pages"] = doc.page_count
        title = (doc.metadata or {}).get("title")
        text, n = [], 0
        for page in doc:
            t = page.get_text()
            text.append(t)
            n += len(t)
            if n > MAX_DOC_CHARS:
                break
    add_doc(res, textio.clean(title) if title else None, textio.clean("\n".join(text)), part)
    if not part:
        res.file["kind"] = "document"


def profile_html(path, res, part=""):
    import trafilatura
    t = textio.read_text(path)
    for before, after in t.fixes:
        res.fix(part or "html", before, after)
    m = re.search(r"<title[^>]*>(.*?)</title>", t.text[:20000], re.S | re.I)
    text = trafilatura.extract(t.text, include_comments=False, include_tables=True) or ""
    add_doc(res, textio.clean(m.group(1).strip()) if m else None, text, part)
    if not part:
        res.file.update(kind="webpage", encoding=t.encoding.encoding, had_bom=t.encoding.had_bom,
                        fixed_count=len(t.fixes), nfc_changed=t.nfc_changed)


def profile_xml(path, res, part=""):
    """KML/GML go to GDAL; anything else (Atom feeds, metadata) is kept as text."""
    with open(path, "rb") as f:
        head = f.read(4096).lower()
    if any(tag in head for tag in (b"<kml", b"opengis.net/gml", b"<gml:", b"wfs:featurecollection")):
        if not part:
            res.file["kind"] = "geo"
        return profile_geo(str(path), res, part)
    t = textio.read_text(path)
    for before, after in t.fixes:
        res.fix(part or "xml", before, after)
    text = re.sub(r"<[^>]+>", " ", t.text)
    add_doc(res, None, re.sub(r"\s+", " ", text), part)
    if not part:
        res.file.update(kind="document", encoding=t.encoding.encoding, fixed_count=len(t.fixes))


def profile_docx(path, res, part=""):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    text = re.sub(r"<w:p[ >]", "\n<w:p ", xml)
    text = re.sub(r"<[^>]+>", "", text)
    add_doc(res, None, textio.clean(text), part)
    if not part:
        res.file["kind"] = "document"


def profile_member(local, member, res, zf):
    """One file inside a zip, extracted to `local` (or None for geo, read in place)."""
    ext = PurePosixPath(member).suffix.lower()
    if ext in RASTER_EXT:
        return profile_raster(str(local), res, member)
    if ext in TABLE_EXT:
        return profile_text_table(local, res, member)
    if ext in SHEET_EXT:
        return profile_sheet_file(str(local), res, member)
    if ext == ".pdf":
        return profile_pdf(str(local), res, member)
    if ext in (".html", ".htm"):
        return profile_html(local, res, member)
    if ext == ".xml":
        return profile_xml(local, res, member)
    if ext == ".docx":
        return profile_docx(local, res, member)
    if ext in (".json", ".geojson"):
        with open(local, "rb") as f:
            head = f.read(2000)
        if b"FeatureCollection" in head or ext == ".geojson":
            return profile_geo(str(local), res, member)
        return None


def profile_zip(path, res, tmpdir):
    abs_path = str(path)
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        names = [i.filename for i in infos]
        res.file["summary"]["members"] = len(names)
        res.file["summary"]["member_list"] = names[:200]
        # Office documents are zips too.
        if "[Content_Types].xml" in names:
            if any(n.startswith("xl/") for n in names):
                return profile_sheet_file(abs_path, res)
            if "word/document.xml" in names:
                return profile_docx(abs_path, res)
        if "mimetype" in names and b"opendocument.spreadsheet" in zf.read("mimetype"):
            res.file.update(kind="table")
            res.file["summary"]["note"] = "ODS spreadsheet, not profiled"
            return
        res.file["kind"] = "archive"
        done, kinds = 0, Counter()
        gdbs = sorted({n.split(".gdb/")[0] + ".gdb" for n in names if ".gdb/" in n.lower()})
        for g in gdbs:
            try:
                profile_geo(f"/vsizip/{abs_path}/{g}", res, g)
                kinds["geo"] += 1
            except Exception as e:
                res.file["summary"].setdefault("warnings", []).append(f"{g}: {e}"[:300])
        for info in infos:
            m = info.filename
            ext = PurePosixPath(m).suffix.lower()
            if done >= MAX_ZIP_MEMBERS or ".gdb/" in m.lower() or "__MACOSX" in m:
                continue
            try:
                if ext in GEO_EXT - {".gdb", ".tab"}:
                    enc = _shapefile_enc(None, zf, m) if ext == ".shp" else None
                    profile_geo(f"/vsizip/{abs_path}/{m}", res, m, enc)
                    kinds["geo"] += 1
                elif ext in TABLE_EXT | SHEET_EXT | RASTER_EXT | DOC_EXT | {".json", ".docx"}:
                    if info.file_size > 4 << 30:
                        continue
                    local = Path(tmpdir) / f"m{done}{ext}"
                    with zf.open(info) as src, open(local, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    profile_member(local, m, res, zf)
                    local.unlink(missing_ok=True)
                    kinds[ext] += 1
                else:
                    continue
                done += 1
            except Exception as e:
                res.file["summary"].setdefault("warnings", []).append(f"{m}: {type(e).__name__}: {e}"[:300])
        if kinds["geo"] or res.layers:
            res.file["kind"] = "geo"
        res.file["summary"]["profiled_members"] = dict(kinds)


def profile_gzip(path, res, tmpdir):
    local = Path(tmpdir) / "inner"
    with gzip.open(path) as src, open(local, "wb") as dst:
        shutil.copyfileobj(src, dst)
    with open(local, "rb") as f:
        inner = textio.sniff_type(f.read(8192))
    res.file["summary"]["inner_type"] = inner
    if inner == "text":
        profile_text_table(local, res, "gunzipped")
        res.file["kind"] = "table"
    else:
        res.file["kind"] = "archive"


# ── multi-file layers saved as separate downloads ────────────────────────────

# A MapInfo layer is .tab + .dat + .id + .map, a shapefile is .shp + .dbf + .shx
# (+ .prj, .cpg). The mirror saved each piece as its own resource, named
# <8-char resource id>_<original name>, so GDAL can't find the companions.
SIDECARS = {".tab": (".dat", ".id", ".map", ".ind"), ".shp": (".dbf", ".shx", ".prj", ".cpg", ".sbn", ".sbx")}
SIDECAR_EXT = {e for v in SIDECARS.values() for e in v}
_PREFIX = re.compile(r"^[0-9a-f]{8}_")


def assemble_layer(path, tmpdir):
    """Link the main file and its companions into tmpdir under their original
    names. Returns the path to open, and the companions found."""
    main_name = _PREFIX.sub("", path.name)
    stem, ext = os.path.splitext(main_name)
    found = []
    for sib in path.parent.iterdir():
        orig = _PREFIX.sub("", sib.name)
        s, e = os.path.splitext(orig)
        if s.lower() == stem.lower() and e.lower() in SIDECARS[ext.lower()]:
            os.symlink(sib, Path(tmpdir) / (stem + e.lower()))
            found.append(e.lower())
    target = Path(tmpdir) / (stem + ext.lower())
    os.symlink(path, target)
    return target, found


def has_main(path):
    """Is this .dat/.dbf/... a companion of a .tab or .shp in the same folder?"""
    stem = os.path.splitext(_PREFIX.sub("", path.name))[0].lower()
    ext = path.suffix.lower()
    for sib in path.parent.iterdir():
        s, e = os.path.splitext(_PREFIX.sub("", sib.name))
        if s.lower() == stem and ext in SIDECARS.get(e.lower(), ()):
            return True
    return False


def profile_assembled(path, res, tmpdir, ext):
    target, found = assemble_layer(path, tmpdir)
    res.file["kind"] = "geo"
    res.file["summary"]["companions"] = sorted(found)
    missing = {".tab": {".dat", ".map"}, ".shp": {".shx"}}[ext] - set(found)
    if missing:
        raise RuntimeError(f"incomplete layer: companion file(s) {', '.join(sorted(missing))} "
                           "not in the mirror")
    enc = _shapefile_enc(str(target)) if ext == ".shp" else None
    profile_geo(str(target), res, "", enc)


# ── one file ─────────────────────────────────────────────────────────────────

def profile_file(task):
    sha256, rel, size = task
    res = Result(sha256, rel, size)
    path = db.MIRROR / rel
    f = res.file
    tmpdir = None
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
        real = textio.sniff_type(head)
        f["real_type"] = real
        ext = path.suffix.lower()
        if real == "empty":
            f["kind"] = "empty"
        elif ext == ".shp" or (ext == ".tab" and head.lstrip().lower().startswith(b"!table")):
            TMP_ROOT.mkdir(parents=True, exist_ok=True)
            tmpdir = tempfile.mkdtemp(dir=TMP_ROOT)
            profile_assembled(path, res, tmpdir, ext)
        elif ext in SIDECAR_EXT and real in ("binary", "text") and has_main(path):
            # A companion of a layer profiled through its .shp / .tab.
            f["kind"] = "sidecar"
        elif real == "zip":
            TMP_ROOT.mkdir(parents=True, exist_ok=True)
            tmpdir = tempfile.mkdtemp(dir=TMP_ROOT)
            if ext == ".kmz":
                f["kind"] = "geo"
                profile_geo(str(path), res, "")
            else:
                profile_zip(path, res, tmpdir)
        elif real == "gzip":
            TMP_ROOT.mkdir(parents=True, exist_ok=True)
            tmpdir = tempfile.mkdtemp(dir=TMP_ROOT)
            profile_gzip(path, res, tmpdir)
        elif real == "pdf":
            profile_pdf(str(path), res)
        elif real == "ole":
            profile_sheet_file(str(path), res)
        elif real == "sqlite":
            f["kind"] = "geo"
            profile_geo(str(path), res, "")
        elif real in ("tiff", "image"):
            f["kind"] = "raster"
            profile_raster(str(path), res, "")
        elif real == "html":
            profile_html(path, res)
        elif real == "xml":
            profile_xml(path, res)
        elif real == "json":
            profile_json(path, res)
        elif real in ("text", "utf16-text"):
            if ext in (".shp", ".dbf", ".shx"):
                f["kind"] = "binary"
            else:
                profile_text_table(path, res)
        else:
            if ext == ".shp":
                f["kind"] = "geo"
                profile_geo(str(path), res, "", _shapefile_enc(str(path)))
            else:
                f["kind"] = "binary"
    except Exception as e:
        f["error"] = f"{type(e).__name__}: {e}"[:1000]
        f.setdefault("kind", "error")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    f.setdefault("kind", "unknown")
    f["fixed_count"] = max(f.get("fixed_count") or 0, len(res.fixes))
    return res


# ── writing ──────────────────────────────────────────────────────────────────

def write(conn, results):
    with conn.cursor() as cur:
        shas = [r.file["sha256"] for r in results]
        cur.execute("DELETE FROM dgi_prof.file WHERE sha256 = ANY(%s)", (shas,))
        cur.execute("DELETE FROM dgi_prof.text_fix WHERE source = 'file' AND ref = ANY(%s)",
                    ([r.file["path"] for r in results],))
        for r in results:
            f = r.file
            cur.execute("""
                INSERT INTO dgi_prof.file (sha256, path, bytes, real_type, kind, encoding, had_bom, mixed,
                                           nfc_changed, fixed_count, n_rows, n_cols, summary, error, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (f["sha256"], f["path"], f["bytes"], f.get("real_type"), f.get("kind"), f.get("encoding"),
                 f.get("had_bom"), f.get("mixed"), f.get("nfc_changed"), f.get("fixed_count"),
                 f.get("n_rows"), f.get("n_cols"), json.dumps(f["summary"], ensure_ascii=False),
                 f.get("error"), VERSION))
            seen = set()
            for c in r.cols:
                key = (c["part"], c["ordinal"])
                if key in seen:
                    continue
                seen.add(key)
                cur.execute("""
                    INSERT INTO dgi_prof.col (sha256, part, ordinal, name, role, inferred_type, null_frac,
                                              n_distinct, distinct_capped, top_values, samples, distinct_values)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (f["sha256"], c["part"], c["ordinal"], c["name"], c["role"], c["inferred_type"],
                     c["null_frac"], c["n_distinct"], c["distinct_capped"],
                     json.dumps(c["top_values"], ensure_ascii=False) if c["top_values"] is not None else None,
                     c["samples"], c.get("distinct_values")))
            seen = set()
            for lay in r.layers:
                key = (lay["part"], lay["layer"])
                if key in seen:
                    continue
                seen.add(key)
                cur.execute("""
                    INSERT INTO dgi_prof.layer (sha256, part, layer, gdal_path, geom_type, feature_count, srid,
                                                srs_name, extent, fields, sample, encoding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (f["sha256"], lay["part"], lay["layer"], lay["gdal_path"], lay["geom_type"],
                     lay["feature_count"], lay["srid"], lay["srs_name"], lay["extent"],
                     json.dumps(lay["fields"], ensure_ascii=False),
                     json.dumps(lay["sample"], ensure_ascii=False) if lay["sample"] is not None else None,
                     lay["encoding"]))
            if r.jsonstat:
                j = r.jsonstat
                cur.execute("""
                    INSERT INTO dgi_prof.jsonstat (sha256, matrix, label, updated, version, n_values, dims,
                                                   units, notes, extension)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (f["sha256"], j["matrix"], j["label"], j["updated"], j["version"], j["n_values"],
                     json.dumps(j["dims"], ensure_ascii=False), json.dumps(j["units"], ensure_ascii=False),
                     [str(n) for n in j["notes"]], json.dumps(j["extension"], ensure_ascii=False)))
            if r.doc:
                cur.execute("INSERT INTO dgi_prof.doc_text (sha256, title, chars, text) VALUES (%s, %s, %s, %s)",
                            (f["sha256"], r.doc["title"], r.doc["chars"], r.doc["text"].replace("\x00", "")))
            for path, field, before, after in r.fixes:
                cur.execute("INSERT INTO dgi_prof.text_fix (source, ref, field, before, after) "
                            "VALUES ('file', %s, %s, %s, %s)",
                            (path, field, before[:2000], after[:2000]))
    conn.commit()


def work_list(conn, redo=False, kinds=None, limit=None, sha=None, failed=False):
    q = """
        SELECT DISTINCT ON (r.sha256) r.sha256, r.path, r.bytes
          FROM dgi_cat.resource r
         WHERE r.status = 'done' AND r.path IS NOT NULL AND r.sha256 IS NOT NULL"""
    params = []
    if not redo:
        q += " AND NOT EXISTS (SELECT FROM dgi_prof.file p WHERE p.sha256 = r.sha256 AND p.version = %s)"
        params.append(VERSION)
    if sha:
        q += " AND r.sha256 = %s"
        params.append(sha)
    if failed:
        q += " AND EXISTS (SELECT FROM dgi_prof.file p WHERE p.sha256 = r.sha256 AND p.error IS NOT NULL)"
    if kinds:
        q += " AND EXISTS (SELECT FROM dgi_prof.file p WHERE p.sha256 = r.sha256 AND p.kind = ANY(%s))"
        params.append(kinds)
    q += " ORDER BY r.sha256, r.path"
    rows = conn.execute(q, params).fetchall()
    # Small files first, so progress shows early; the big ones run at the end.
    rows.sort(key=lambda r: r[2] or 0)
    return rows[:limit] if limit else rows


def run(dry_run=False, workers=12, limit=None, redo=False, kinds=None, sha=None, failed=False):
    with db.connect() as conn:
        db.run_sql_files(conn, "00_schemas", "15_prof")
        conn.commit()
        tasks = work_list(conn, redo=redo or failed, kinds=kinds, limit=limit, sha=sha, failed=failed)
        total_bytes = sum(t[2] or 0 for t in tasks)
        log.info("%d files to profile (%.1f GB)", len(tasks), total_bytes / 1e9)
        if dry_run or not tasks:
            return
        batch, n, kinds_seen, errors = [], 0, Counter(), 0
        with Pool(workers, maxtasksperchild=200) as pool:
            for res in pool.imap_unordered(profile_file, tasks, chunksize=4):
                batch.append(res)
                n += 1
                kinds_seen[res.file.get("kind")] += 1
                errors += bool(res.file.get("error"))
                if len(batch) >= 100:
                    write(conn, batch)
                    batch = []
                if n % 1000 == 0:
                    log.info("profiled %d/%d  %s  errors %d", n, len(tasks), dict(kinds_seen.most_common(8)), errors)
            if batch:
                write(conn, batch)
        log.info("done: %d files  %s  errors %d", n, dict(kinds_seen), errors)

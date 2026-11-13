"""Phase 6a: the static tree browser at /var/www/html/dgi/ (http://scierstar/dgi/).

Pages: an index, publishers, themes, location levels, and one page per
dataset with its metadata, every file (what it really is, its encoding notes,
a preview of columns / CSO dimensions / map fields), the datasets it can be
joined with, and the most similar datasets by meaning.

Built into a fresh folder and swapped in at the end, so the live site is
never half-written. All pages are UTF-8.
"""

import html
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path

import jinja2

from . import db

log = logging.getLogger("dgi")

WEB = Path("/var/www/html/dgi")
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
LEVEL_NAMES = {"state": "State", "province": "Province", "nuts2": "NUTS2 region", "nuts3": "NUTS3 region",
               "county": "County", "la": "Local authority", "lea": "Local electoral area",
               "ed": "Electoral division", "sa": "Small area"}
LEVEL_ORDER = list(LEVEL_NAMES)
HOW = {"same_cso_dim": "Same CSO classification", "same_geo_level": "Same location level",
       "same_places": "Map layers in the same councils"}


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "unknown").lower()).strip("-")
    return s[:80] or "unknown"


def size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _table(rows, head=None):
    out = ["<table>"]
    if head:
        out.append("<tr>" + "".join(f"<th>{html.escape(str(h))}</th>" for h in head) + "</tr>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{html.escape(str(c) if c is not None else '')}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def gather(conn):
    q = lambda s, *a: conn.execute(s, a).fetchall()
    log.info("reading the catalogue")
    ds = {}
    for r in q("""
        SELECT d.id, d.title, d.title_ga, d.description, p.name, d.themes, d.keywords, d.periodicity,
               d.licence, d.contact_email, d.temporal_start, d.temporal_end
          FROM dgi_cat.dataset d LEFT JOIN dgi_cat.publisher p ON p.id = d.publisher_id"""):
        ds[r[0]] = dict(id=r[0], title=r[1], title_ga=r[2], description=r[3], publisher=r[4],
                        publisher_slug=slug(r[4]), themes=", ".join(r[5] or []), theme_list=r[5] or [],
                        keywords=", ".join(r[6] or []), periodicity=r[7], licence=r[8], contact=r[9],
                        time=f"{r[10]:%Y}–{r[11]:%Y}" if r[10] and r[11] else "", geo="", places="",
                        geo_levels=[], kinds="")
    for did, concept, level, starts, ends in q("SELECT dataset_id, concept, level, starts, ends FROM dgi_link.dataset_key"):
        d = ds.get(did)
        if not d:
            continue
        if concept == "geo":
            d["geo_levels"].append(level)
        elif starts and ends:
            d.setdefault("_t", []).append((starts, ends, level))
    for d in ds.values():
        if d.get("_t"):
            s = min(t[0] for t in d["_t"]); e = max(t[1] for t in d["_t"])
            d["time"] = f"{s:%Y}–{e:%Y} ({', '.join(sorted({t[2] for t in d['_t']}))})"
        d["geo_levels"].sort(key=lambda l: LEVEL_ORDER.index(l) if l in LEVEL_ORDER else 99)
        d["geo"] = ", ".join(LEVEL_NAMES.get(l, l) for l in d["geo_levels"])

    la = dict(q("SELECT code, name FROM dgi_ref.area WHERE level = 'la'"))
    places = defaultdict(set)
    for did, code in q("""
        SELECT DISTINCT r.dataset_id, e.la_code
          FROM dgi_link.layer_place_exact e
               JOIN dgi_geo.layer_table t USING (table_name)
               JOIN dgi_cat.resource r ON r.sha256 = t.sha256
         WHERE e.share >= 0.05"""):
        places[did].add(la.get(code, code))
    for did, p in places.items():
        if did in ds:
            ds[did]["places"] = ", ".join(sorted(p))

    log.info("reading files and previews")
    files = defaultdict(list)
    kinds = defaultdict(set)
    for r in q("""
        SELECT r.dataset_id, r.title, r.format, r.status, r.path, r.bytes, r.sha256,
               f.kind, f.encoding, f.had_bom, f.mixed, f.fixed_count, f.n_rows, f.n_cols, f.error,
               f.summary->>'note'
          FROM dgi_cat.resource r LEFT JOIN dgi_prof.file f ON f.sha256 = r.sha256
      ORDER BY r.dataset_id, r.title"""):
        (did, title, fmt, status, path, nbytes, sha, kind, enc, bom, mixed, fixed, nrows, ncols, err, note) = r
        notes = []
        if enc in ("cp1252", "mixed", "utf-16", "utf-16-le", "utf-16-be"):
            notes.append({"cp1252": "Windows-1252 text, converted", "mixed": "mixed encodings, converted line by line"}
                         .get(enc, "UTF-16 text, converted"))
        if fixed:
            notes.append(f"{fixed} double-encoded values repaired")
        if nrows and kind in ("table", "jsonstat"):
            notes.append(f"{nrows:,} {'values' if kind == 'jsonstat' else 'rows'}")
        if err:
            notes.append("could not be read: " + err[:120])
        if note:
            notes.append(note)
        href = "/geo/mirror/" + path[len("files/"):] if path and path.startswith("files/") else None
        is_geojson = path and path.lower().endswith(".geojson")
        files[did].append(dict(title=title, name=os.path.basename(path or ""), format=fmt, status=status,
                               size=size(nbytes), kind=kind, notes="; ".join(notes), href=href, sha=sha,
                               map=f"/geo/viewer/?url={href}" if href and is_geojson else None,
                               server=str(db.MIRROR / path) if path else None,
                               previews=[], table=None, members=[], queries=[]))
        if path and did in ds and not ds[did].get("folder"):
            ds[did]["folder"] = str((db.MIRROR / path).parent)
        if kind:
            kinds[did].add(kind)
    for did, k in kinds.items():
        if did in ds:
            ds[did]["kinds"] = ", ".join(sorted(k))

    previews = defaultdict(list)
    tables = {}
    for sha, part, name, typ, samples in q("""
        SELECT c.sha256, c.part, c.name, c.inferred_type, c.samples
          FROM dgi_prof.col c JOIN dgi_prof.file f USING (sha256)
         WHERE f.kind = 'table' ORDER BY c.sha256, c.part, c.ordinal"""):
        tables.setdefault(sha, []).append((part, name, typ, ", ".join((samples or [])[:3])))
    for sha, rows in tables.items():
        previews[sha].append(_table([(n, t, s) for _, n, t, s in rows[:30]], ["Column", "Type", "Examples"]))
    for sha, matrix, dims in q("SELECT sha256, matrix, dims FROM dgi_prof.jsonstat"):
        previews[sha].append(_table([(d.get("label"), d.get("size"), ", ".join(list((d.get("categories") or {}).values())[:6]))
                                     for d in dims], ["Dimension", "Categories", "First few"]))
    for sha, layer, gtype, n, fields, tname in q("""
        SELECT l.sha256, l.layer, l.geom_type, l.feature_count, l.fields, t.table_name
          FROM dgi_prof.layer l LEFT JOIN dgi_geo.layer_table t USING (sha256, part, layer)
         WHERE l.geom_type IS NOT NULL"""):
        flds = ", ".join(f[0] for f in (fields or [])[:25])
        previews[sha].append(_table([(layer, gtype, n, flds, f"dgi_geo.{tname}" if tname else "")],
                                    ["Map layer", "Geometry", "Features", "Fields", "In PostGIS"]))
    log.info("reading where the data is in Postgres")
    queries = defaultdict(list)          # sha -> [(label, query, rows)]
    for sha, matrix, n in q("SELECT sha256, matrix, n_values FROM dgi_data.cso_table"):
        queries[sha].append((f"CSO table {matrix}", f"SELECT * FROM dgi_data.cso('{matrix}') LIMIT 100;", n))
    for sha, t, member, n in q("""SELECT sha256, table_name, member, rows FROM dgi_data.csv_table
                                   WHERE error IS NULL ORDER BY member"""):
        queries[sha].append((member or "CSV", f"SELECT * FROM dgi_data.{t} LIMIT 100;", n))
    for sha, t, layer, n in q("""SELECT sha256, table_name, layer, features FROM dgi_geo.layer_table
                                  WHERE error IS NULL ORDER BY layer"""):
        queries[sha].append((f"map layer {layer}", f"SELECT * FROM dgi_geo.{t} LIMIT 100;", n))
    members = {sha: m for sha, m in q("""SELECT sha256, summary->'member_list' FROM dgi_prof.file
                                          WHERE real_type = 'zip' AND summary ? 'member_list'""")}
    views = defaultdict(list)
    for did, v, nfiles, n in q("""SELECT dataset_id, view_name, array_length(files, 1), rows
                                    FROM dgi_data.dataset_view ORDER BY view_name"""):
        views[did].append(dict(label=f"Whole dataset: {nfiles} file(s) stacked, with a source_file column",
                               query=f"SELECT * FROM dgi_data.{v} LIMIT 100;", rows=n))
    for did, lst in files.items():
        for f in lst:
            f["previews"] = previews.get(f["sha"], [])[:6]
            f["queries"] = [dict(label=l, query=qq, rows=n) for l, qq, n in queries.get(f["sha"], [])]
            f["members"] = (members.get(f["sha"]) or [])[:60]
        if did in ds:
            ds[did]["queries"] = views.get(did, []) + [qq for f in lst for qq in f["queries"]][:40]

    log.info("reading similar datasets")
    similar = defaultdict(list)
    try:
        for did, oid in q("SELECT dataset_id, other_id FROM dgi_vec.similar ORDER BY dataset_id, rank"):
            if oid in ds:
                similar[did].append(dict(id=oid, title=ds[oid]["title"], publisher=ds[oid]["publisher"]))
    except Exception:
        conn.rollback()
    return ds, files, similar


def joinables(conn, ds, top=15):
    """Top joinable datasets for every dataset that has a location key.

    Candidates come from dgi_link.joinable_with (same CSO classification, same
    location level, same councils, overlapping years). Many tie, so they're
    re-ranked by meaning: the cosine similarity of the two datasets' cards.
    """
    import numpy as np
    vec = {}
    for did, emb in conn.execute("SELECT dataset_id, emb FROM dgi_vec.item WHERE kind = 'dataset' AND emb IS NOT NULL"):
        v = np.asarray(emb, dtype=np.float32)
        vec[did] = v / np.linalg.norm(v)
    out = {}
    ids = [d for d, v in ds.items() if v["geo_levels"] or v["places"]]
    log.info("finding joinable datasets for %d datasets", len(ids))
    for i, did in enumerate(ids):
        rows = conn.execute("SELECT dataset_id, title, how, years, score FROM dgi_link.joinable_with(%s, 300)",
                            (did,)).fetchall()
        me = vec.get(did)
        ranked = sorted(rows, key=lambda r: -(r[4] + (2 * float(me @ vec[r[0]]) if me is not None and r[0] in vec else 0)))
        out[did] = [dict(id=r[0], title=r[1], how=HOW.get(r[2], r[2]), years=r[3]) for r in ranked[:top]]
        if i % 2000 == 1999:
            log.info("  %d/%d", i + 1, len(ids))
    return out


def build(dry_run=False):
    with db.connect() as conn:
        ds, files, similar = gather(conn)
        join = joinables(conn, ds)
    if dry_run:
        log.info("would write %d dataset pages", len(ds))
        return
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    built = time.strftime("%Y-%m-%d %H:%M")
    tmp = WEB.with_name("dgi.new")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "d").mkdir(parents=True)
    (tmp / "p").mkdir()
    (tmp / "t").mkdir()
    (tmp / "l").mkdir()
    shutil.copy(TEMPLATES / "style.css", tmp / "style.css")

    def write(path, template, **kw):
        (tmp / path).write_text(env.get_template(template).render(built=built, **kw), encoding="utf-8")

    by_pub, by_theme, by_level = defaultdict(list), defaultdict(list), defaultdict(list)
    for d in ds.values():
        by_pub[(d["publisher"] or "Unknown publisher", d["publisher_slug"])].append(d)
        for t in d["theme_list"]:
            by_theme[t].append(d)
        for l in d["geo_levels"]:
            by_level[l].append(d)
        write(f"d/{d['id']}.html", "dataset.html", root="../", title=d["title"] or "(no title)", d=d,
              files=files.get(d["id"], []), joinable=join.get(d["id"]), similar=similar.get(d["id"]))
    key = lambda d: (d["title"] or "").lower()
    for (name, s), lst in by_pub.items():
        write(f"p/{s}.html", "list.html", root="../", title=name, heading=name,
              intro=f"{len(lst):,} datasets", datasets=sorted(lst, key=key))
    for t, lst in by_theme.items():
        write(f"t/{slug(t)}.html", "list.html", root="../", title=t, heading=f"Theme: {t}",
              intro=f"{len(lst):,} datasets", datasets=sorted(lst, key=key))
    for l, lst in by_level.items():
        write(f"l/{l}.html", "list.html", root="../", title=LEVEL_NAMES.get(l, l),
              heading=f"Keyed by {LEVEL_NAMES.get(l, l).lower()}",
              intro=f"{len(lst):,} datasets have a column or dimension naming areas at this level.",
              datasets=sorted(lst, key=key))
    write("publishers.html", "list.html", root="", title="Publishers", heading="Publishers",
          groups=[dict(href=f"p/{s}.html", name=n, n=len(v)) for (n, s), v in sorted(by_pub.items())])
    write("themes.html", "list.html", root="", title="Themes", heading="Themes",
          groups=[dict(href=f"t/{slug(t)}.html", name=t, n=len(v))
                  for t, v in sorted(by_theme.items(), key=lambda kv: -len(kv[1]))])
    write("places.html", "list.html", root="", title="By location level", heading="Datasets by location level",
          intro="Datasets that can be lined up by place, from the whole state down to small areas.",
          groups=[dict(href=f"l/{l}.html", name=LEVEL_NAMES[l], n=len(by_level.get(l, [])))
                  for l in LEVEL_ORDER if by_level.get(l)])
    write("index.html", "list.html", root="", title="Home", heading="data.gov.ie mirror",
          intro=f"{len(ds):,} datasets from {len(by_pub)} publishers. Browse below, or use Search for meaning-based search.",
          groups=[dict(href="publishers.html", name="Publishers", n=len(by_pub)),
                  dict(href="themes.html", name="Themes", n=len(by_theme)),
                  dict(href="places.html", name="By location level", n=sum(1 for d in ds.values() if d["geo_levels"]))])
    old = WEB.with_name("dgi.old")
    shutil.rmtree(old, ignore_errors=True)
    if WEB.exists():
        WEB.rename(old)
    tmp.rename(WEB)
    shutil.rmtree(old, ignore_errors=True)
    log.info("site written to %s: %d dataset pages", WEB, len(ds))

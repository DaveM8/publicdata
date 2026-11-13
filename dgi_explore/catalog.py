"""Phase 1: load the catalogue into dgi_cat.

Sources:
- the mirror's state.sqlite: every dataset and resource, with download
  status, local path and checksum;
- the newest catalogue/<date>/page-NNNN.jsonld.gz: the DCAT metadata
  (description, themes, keywords, time period, footprint, licence, contact)
  that state.sqlite doesn't keep.

The catalogue is small (about 23k datasets, 100k resources), so each run
replaces it completely inside one transaction. Readers never see it half loaded.
"""

import gzip
import json
import logging

from . import db, textio

log = logging.getLogger("dgi")

DCT = "http://purl.org/dc/terms/"
DCAT = "http://www.w3.org/ns/dcat#"
FOAF = "http://xmlns.com/foaf/0.1/"
VCARD = "http://www.w3.org/2006/vcard/ns#"
LOCN = "http://www.w3.org/ns/locn#"
THEME_PREFIX = "http://publications.europa.eu/resource/authority/data-theme/"
LANG_PREFIX = "http://publications.europa.eu/resource/authority/language/"


class Cleaner:
    """textio.clean() that remembers every value the mojibake repair changed."""

    def __init__(self):
        self.fixes = []

    def __call__(self, s, ref, fld):
        if s is None:
            return None
        out = textio.clean(s).strip()
        if textio.MOJIBAKE.search(s) and out != s.strip():
            self.fixes.append(("catalogue", ref, fld, s, out))
        return out or None


def latest_catalogue_dir():
    """The newest catalogue/<date>/ whose walk finished.

    While the mirror is still walking the catalogue (meta.catalogue_resume is
    set), the newest folder is incomplete, so use the one before it.
    """
    dirs = sorted(p for p in (db.MIRROR / "catalogue").iterdir() if p.is_dir())
    with db.mirror_state() as st:
        resume = st.execute("SELECT v FROM meta WHERE k = 'catalogue_resume'").fetchone()
    if resume and resume[0] and len(dirs) > 1:
        log.info("catalogue walk in progress; using %s, not %s", dirs[-2].name, dirs[-1].name)
        return dirs[-2]
    return dirs[-1]


def _vals(node, prop):
    return node.get(prop, []) if node else []


def _lit(node, prop, lang=None):
    """First literal value of a property, preferring the given language."""
    vals = [v for v in _vals(node, prop) if "@value" in v]
    if lang:
        for v in vals:
            if v.get("@language") == lang:
                return v["@value"]
        if any("@language" in v for v in vals):
            # Untagged values count as English; an Irish-only title isn't the English one.
            vals = [v for v in vals if v.get("@language") in (None, "en")] if lang == "en" else []
    return vals[0]["@value"] if vals else None


def _ids(node, prop):
    return [v["@id"] for v in _vals(node, prop) if "@id" in v]


def _uuid(uri):
    return uri.rstrip("/").rsplit("/", 1)[-1] if uri else None


def parse_catalogue(cat_dir):
    """Yield ('dataset', dict), ('resource', dict) and ('publisher', id, name) from every page."""
    for page in sorted(cat_dir.glob("page-*.jsonld.gz")):
        doc = json.load(gzip.open(page))
        graph = doc.get("@graph", doc) if isinstance(doc, dict) else doc
        nodes = {n["@id"]: n for n in graph if "@id" in n}
        for n in graph:
            types = n.get("@type", [])
            if FOAF + "Agent" in types or FOAF + "Organization" in types:
                name = _lit(n, FOAF + "name")
                if name:
                    yield ("publisher", n["@id"], name)
            if DCAT + "Dataset" in types:
                yield ("dataset", _dataset(n, nodes))
            if DCAT + "Distribution" in types:
                yield ("resource", _distribution(n))


def _dataset(n, nodes):
    temporal = nodes.get((_ids(n, DCT + "temporal") or [None])[0])
    spatial = nodes.get((_ids(n, DCT + "spatial") or [None])[0])
    contact = nodes.get((_ids(n, DCAT + "contactPoint") or [None])[0])
    email = (_ids(contact, VCARD + "hasEmail") or [None])[0]
    wkt = _lit(spatial, LOCN + "geometry") or _lit(spatial, DCAT + "bbox")
    themes = []
    for v in _vals(n, DCAT + "theme"):
        t = v.get("@id") or v.get("@value")
        if t:
            themes.append(t.removeprefix(THEME_PREFIX))
    provenance = nodes.get((_ids(n, DCT + "provenance") or [None])[0])
    return {
        "id": _uuid(n["@id"]),
        "uri": n["@id"],
        "title": _lit(n, DCT + "title", "en"),
        "title_ga": _lit(n, DCT + "title", "ga"),
        "description": _lit(n, DCT + "description", "en") or _lit(n, DCT + "description"),
        "publisher_uri": (_ids(n, DCT + "publisher") or [None])[0],
        "themes": themes,
        "keywords": [v["@value"] for v in _vals(n, DCAT + "keyword") if "@value" in v],
        "periodicity": _lit(n, DCT + "accrualPeriodicity") or (_ids(n, DCT + "accrualPeriodicity") or [None])[0],
        "created": _lit(n, DCT + "created"),
        "issued": _lit(n, DCT + "issued"),
        "modified": _lit(n, DCT + "modified"),
        "temporal_start": _lit(temporal, DCAT + "startDate"),
        "temporal_end": _lit(temporal, DCAT + "endDate"),
        "spatial_wkt": wkt,
        "languages": [i.removeprefix(LANG_PREFIX) for i in _ids(n, DCT + "language")],
        "contact_email": email.removeprefix("mailto:") if email else None,
        "conforms_to": _ids(n, DCT + "conformsTo"),
        "provenance": _lit(provenance, "http://www.w3.org/2000/01/rdf-schema#label") if provenance else None,
        "identifier": _lit(n, DCT + "identifier"),
        "jsonld": n,
    }


def _distribution(n):
    return {
        "id": _uuid(n["@id"]),
        "description": _lit(n, DCT + "description"),
        "licence": (_ids(n, DCT + "license") or [None])[0],
        "issued": _lit(n, DCT + "issued"),
        "modified": _lit(n, DCT + "modified"),
    }


def run(dry_run=False):
    cat_dir = latest_catalogue_dir()
    log.info("reading catalogue pages from %s", cat_dir)
    clean = Cleaner()

    agents, cat_ds, cat_res = {}, {}, {}
    for item in parse_catalogue(cat_dir):
        if item[0] == "publisher":
            agents[item[1]] = item[2]
        elif item[0] == "dataset":
            cat_ds[item[1]["id"]] = item[1]
        else:
            cat_res[item[1]["id"]] = item[1]
    log.info("catalogue: %d datasets, %d distributions, %d publishers",
             len(cat_ds), len(cat_res), len(agents))

    with db.mirror_state() as st:
        st_ds = {r["id"]: dict(r) for r in st.execute("SELECT * FROM datasets")}
        st_res = [dict(r) for r in st.execute("SELECT * FROM resources")]
    log.info("state.sqlite: %d datasets, %d resources", len(st_ds), len(st_res))

    # Publishers: the catalogue's own organisation names; else state.sqlite's name.
    publishers = {}
    ds_rows = []
    for did in sorted(set(st_ds) | set(cat_ds)):
        c, s = cat_ds.get(did, {}), st_ds.get(did, {})
        puri = c.get("publisher_uri")
        if puri and puri in agents:
            # Tailte Éireann's datasets name their publisher with a one-off blank node
            # (_:N…) instead of an organisation URI; group those by name.
            pid, pname = (puri if not puri.startswith("_:") else "name:" + agents[puri]), agents[puri]
        elif s.get("publisher"):
            pid, pname = "name:" + s["publisher"], s["publisher"]
        else:
            pid, pname = None, None
        if pid:
            publishers[pid] = clean(pname, pid, "name")
        title = clean(c.get("title") or s.get("title"), did, "title")
        title_ga = clean(c.get("title_ga"), did, "title_ga")
        desc = clean(c.get("description"), did, "description")
        keywords = [k for k in (clean(k, did, "keyword") for k in c.get("keywords", [])) if k]
        search_doc = " ".join(textio.search_key(x) for x in
                              [title, title_ga, desc, publishers.get(pid), *keywords] if x)
        ds_rows.append((
            did, c.get("uri"), title, title_ga, desc, pid, c.get("themes") or None,
            keywords or None, c.get("periodicity"), c.get("created"), c.get("issued"),
            c.get("modified") or s.get("modified"), c.get("temporal_start"), c.get("temporal_end"),
            c.get("spatial_wkt"), c.get("languages") or None,
            None, c.get("contact_email"), c.get("conforms_to") or None, c.get("provenance"),
            c.get("identifier") or s.get("identifier"), did in cat_ds,
            json.dumps(c["jsonld"], ensure_ascii=False) if c else None, search_doc))

    res_rows = []
    for r in st_res:
        d = cat_res.get(r["id"], {})
        res_rows.append((
            r["id"], r["dataset_id"], clean(r["title"], r["id"], "title"),
            clean(d.get("description"), r["id"], "description"), r["format"], r["media_type"],
            r["url"], r["host"], r["kind"], r["status"], r["path"], r["bytes"], r["sha256"],
            r["fetched_at"], r["source"], d.get("licence"), d.get("issued"),
            d.get("modified") or r["modified"]))

    # A dataset's licence: the one its files share, when they all agree.
    lic = {}
    for row in res_rows:
        if row[15]:
            lic.setdefault(row[1], set()).add(row[15])
    ds_rows = [(*row[:16], next(iter(lic[row[0]])) if len(lic.get(row[0], ())) == 1 else None, *row[17:])
               for row in ds_rows]

    log.info("prepared %d publishers, %d datasets, %d resources; %d text values repaired",
             len(publishers), len(ds_rows), len(res_rows), len(clean.fixes))
    if dry_run:
        for f in clean.fixes[:20]:
            log.info("would repair %s %s: %r -> %r", f[1], f[2], f[3], f[4])
        return

    with db.connect() as conn:
        db.run_sql_files(conn, "00_schemas", "10_cat")
        conn.execute("TRUNCATE dgi_cat.resource, dgi_cat.dataset, dgi_cat.publisher")
        conn.execute("DELETE FROM dgi_prof.text_fix WHERE source = 'catalogue'")
        with conn.cursor() as cur:
            with cur.copy("COPY dgi_cat.publisher (id, name) FROM STDIN") as cp:
                for pid, name in publishers.items():
                    cp.write_row((pid, name))
            # Stage dates and footprints as text, then convert leniently: one malformed
            # value in the catalogue becomes NULL instead of aborting the load.
            cur.execute("""
                CREATE TEMP TABLE ds_load (
                    id text, uri text, title text, title_ga text, description text, publisher_id text,
                    themes text[], keywords text[], periodicity text, created text, issued text,
                    modified text, temporal_start text, temporal_end text, spatial_wkt text,
                    languages text[], licence text, contact_email text, conforms_to text[],
                    provenance text, identifier text, in_latest_catalogue boolean, jsonld jsonb,
                    search_doc text) ON COMMIT DROP""")
            with cur.copy("COPY ds_load FROM STDIN") as cp:
                for row in ds_rows:
                    cp.write_row(row)
            cur.execute("""
                INSERT INTO dgi_cat.dataset
                      (id, uri, title, title_ga, description, publisher_id, themes, keywords,
                       periodicity, created, issued, modified, temporal_start, temporal_end, spatial,
                       languages, licence, contact_email, conforms_to, provenance, identifier,
                       in_latest_catalogue, jsonld, search_doc)
                SELECT id, uri, title, title_ga, description, publisher_id, themes, keywords,
                       periodicity, dgi_cat.safe_ts(created), dgi_cat.safe_ts(issued),
                       dgi_cat.safe_ts(modified), dgi_cat.safe_date(temporal_start),
                       dgi_cat.safe_date(temporal_end), dgi_cat.safe_wkt(spatial_wkt),
                       languages, licence, contact_email, conforms_to, provenance, identifier,
                       in_latest_catalogue, jsonld, search_doc
                  FROM ds_load""")
            cur.execute("""
                CREATE TEMP TABLE res_load (
                    id text, dataset_id text, title text, description text, format text,
                    media_type text, url text, host text, kind text, status text, path text,
                    bytes bigint, sha256 text, fetched_at text, source text, licence text,
                    issued text, modified text) ON COMMIT DROP""")
            with cur.copy("COPY res_load FROM STDIN") as cp:
                for row in res_rows:
                    cp.write_row(row)
            cur.execute("""
                INSERT INTO dgi_cat.resource
                SELECT id, dataset_id, title, description, format, media_type, url, host, kind,
                       status, path, bytes, sha256, dgi_cat.safe_ts(fetched_at), source, licence,
                       dgi_cat.safe_ts(issued), dgi_cat.safe_ts(modified)
                  FROM res_load""")
            with cur.copy("COPY dgi_prof.text_fix (source, ref, field, before, after) FROM STDIN") as cp:
                for f in clean.fixes:
                    cp.write_row(f)
            cur.execute("INSERT INTO dgi_cat.load_log (catalogue_dir, datasets, resources, publishers, text_fixes) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (cat_dir.name, len(ds_rows), len(res_rows), len(publishers), len(clean.fixes)))
        conn.execute("ANALYZE dgi_cat.dataset; ANALYZE dgi_cat.resource")
    log.info("catalogue loaded")

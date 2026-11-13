"""Phase 7: the nugget finder — strong, unexpected patterns between datasets.

1. Indicators (SQL): every CSO statistic at a location level, other breakdowns
   held at their totals, one value per area per year (dgi_insight.indicator,
   dgi_insight.ind_value).
2. Counts become rates: an indicator whose unit is a count is divided by the
   population of each area (the census population indicator at that level,
   nearest year). Otherwise every count "correlates" with population.
3. For each level and year with at least 20 areas: Spearman rank correlations
   between all indicators covering the same areas (numpy).
4. Surprise: |rho| x (1 - cosine similarity of the two datasets' cards). Two
   tables about the same thing are expected to correlate; a strong link
   between unrelated datasets is the interesting kind.
5. Outliers: areas more than 3 standard deviations from the rest.
6. A report: ~/org/dgi-nuggets-<date>.org, with caveats.

Correlation across areas is not causation, and what holds for areas need not
hold for people in them (the ecological fallacy). Nuggets are leads to check.
"""

import logging
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import db

log = logging.getLogger("dgi")

LEVELS = ("county", "la", "lea", "nuts3", "ed")
MIN_AREAS = 20
MIN_RHO = 0.7
TOP_PER_LEVEL = 400
CENSUS_YEARS = {1926, 1936, 1946, 1951, 1956, 1961, 1966, 1971, 1979, 1981, 1986, 1991, 1996,
                2002, 2006, 2011, 2016, 2022}
COUNT_UNITS = ("number", "persons", "households", "thousand", "count", "no.")

INDICATORS = """
TRUNCATE dgi_insight.indicator, dgi_insight.ind_value;

-- Tables with a location dimension at a level we compare, and a yearly time dimension.
CREATE TEMP TABLE geo_tab AS
  SELECT
         t.matrix_id
       , t.matrix
       , t.label
       , t.geo_dim
       , t.time_dim
       , t.stat_dim
       , k.level
       , (SELECT r.dataset_id FROM dgi_cat.resource r WHERE r.sha256 = t.sha256 LIMIT 1) AS dataset_id
    FROM dgi_data.cso_table t
         JOIN dgi_link.key_column k ON k.sha256 = t.sha256 AND k.concept = 'geo' AND k.ordinal = t.geo_dim
   WHERE k.level = ANY(%(levels)s)
     AND t.time_dim IS NOT NULL;

CREATE TEMP TABLE tot AS SELECT cat_id FROM dgi_insight.total_cat;
CREATE UNIQUE INDEX ON tot (cat_id);

-- Values where every other breakdown is at its total.
CREATE TEMP TABLE v AS
  SELECT
         g.matrix_id
       , g.level
       , v.stat_cat
       , v.time_cat
       , a.code AS area
       , v.value
    FROM geo_tab g
         JOIN dgi_data.cso_value v ON v.matrix_id = g.matrix_id
         JOIN dgi_data.cso_cat_area a ON a.cat_id = v.geo_cat AND a.level = g.level
   WHERE v.value IS NOT NULL
     AND NOT EXISTS (
           SELECT
             FROM unnest(v.cat_ids) WITH ORDINALITY x(c, n)
            WHERE n - 1 NOT IN (g.geo_dim, g.time_dim, coalesce(g.stat_dim, -1))
              AND NOT EXISTS (SELECT FROM tot WHERE tot.cat_id = x.c)
         );

-- Years from the time categories (yearly and year-range periods only).
CREATE TEMP TABLE yr AS
  SELECT
         c.cat_id
       , extract(year FROM p.ends)::int AS year
    FROM dgi_data.cso_cat c
       , dgi_link.period(c.label) p
   WHERE c.cat_id IN (SELECT DISTINCT time_cat FROM v)
     AND p.level IN ('year', 'year_range');

INSERT INTO dgi_insight.indicator (ind_id, matrix_id, matrix, dataset_id, stat_cat, label, unit, level, is_count)
  SELECT
         row_number() OVER (ORDER BY g.matrix, s.stat_cat)
       , g.matrix_id
       , g.matrix
       , g.dataset_id
       , s.stat_cat
       , g.matrix || ' ' || regexp_replace(g.label, '^\\S+ - ', '') || coalesce(': ' || sc.label, '')
       , u.unit
       , g.level
       , lower(coalesce(u.unit, '')) ~ '^(number|persons|households|thousand|count|no\\.)'
    FROM (SELECT DISTINCT matrix_id, stat_cat FROM v) s
         JOIN geo_tab g USING (matrix_id)
         LEFT JOIN dgi_data.cso_cat sc ON sc.cat_id = s.stat_cat
         LEFT JOIN LATERAL (
           SELECT j.units->sc.code->>'label' AS unit
             FROM dgi_prof.jsonstat j
            WHERE j.matrix = g.matrix
            LIMIT 1
         ) u ON true;

INSERT INTO dgi_insight.ind_value (ind_id, year, area, value)
  SELECT
         i.ind_id
       , y.year
       , v.area
       , avg(v.value)
    FROM v
         JOIN yr y ON y.cat_id = v.time_cat
         JOIN dgi_insight.indicator i ON i.matrix_id = v.matrix_id AND i.stat_cat IS NOT DISTINCT FROM v.stat_cat
GROUP BY i.ind_id, y.year, v.area;

ANALYZE dgi_insight.indicator;
ANALYZE dgi_insight.ind_value;
"""


def build_indicators(conn):
    db.run_sql_files(conn, "60_insight")
    t = time.time()
    for stmt in [s for s in INDICATORS.split(";\n") if s.strip()]:
        conn.execute(stmt, {"levels": list(LEVELS)} if "%(levels)s" in stmt else None)
    n = conn.execute("SELECT count(*), (SELECT count(*) FROM dgi_insight.ind_value) FROM dgi_insight.indicator").fetchone()
    conn.commit()
    log.info("indicators: %d, values: %d (%.0fs)", n[0], n[1], time.time() - t)


def _population(conn, level):
    """{year: {area: population}} at this level.

    Among indicators labelled as population, take for each year the one
    covering the most areas and, of those, with the largest total: the whole
    population is bigger than any subset ("aged 15 and over", "not enumerated
    at their usual residence", ...).
    """
    rows = conn.execute("""
        SELECT v.year, i.ind_id, v.area, v.value
          FROM dgi_insight.indicator i JOIN dgi_insight.ind_value v USING (ind_id)
         WHERE i.level = %s
           AND i.label ~* '(population|persons usually resident)'
           AND i.label !~* '(change|density|percent|%%|aged|not enumerated|border|living with|whose|at work|speakers|per county|estimates)'
           AND v.value > 0""", (level,)).fetchall()
    cands = defaultdict(lambda: defaultdict(dict))
    for year, ind_id, area, value in rows:
        cands[year][ind_id][area] = value
    pop = {}
    for year, by_ind in cands.items():
        # Census years only: other years' "population" figures are often partial
        # (labour force, estimates for some areas). Rankings of areas by
        # population change slowly, so other years use the nearest census.
        if year not in CENSUS_YEARS:
            continue
        full = [d for d in by_ind.values() if sum(d.values()) >= 2_500_000]
        if full:
            pop[year] = max(full, key=lambda d: (len(d), sum(d.values())))
    return pop


def _nearest(pop, year):
    if not pop:
        return None
    best = min(pop, key=lambda y: abs(y - year))
    return pop[best] if abs(best - year) <= 6 else None


_NOT_COUNT = ("average", "median", "mean", "rate", "ratio", " per ", "per 1", "index", "percent", "%",
              " age", "length of stay", "years", "density", "share", "proportion", "price", "earnings")


def _is_count(label, x, prank):
    """Is this indicator a count (to divide by population)? The CSO labels
    averages and medians with the unit "Number" too, so decide from the data:
    whole, non-negative numbers whose ranking follows population size, and a
    label that doesn't say average, median, rate, ..."""
    if any(w in (label or "").lower() for w in _NOT_COUNT):
        return False
    if (x < 0).any() or not np.allclose(x, np.round(x)):
        return False
    r = x.argsort().argsort().astype(np.float64)
    r -= r.mean()
    r /= np.linalg.norm(r)
    return float(r @ prank) >= 0.85


def _cards(conn):
    rows = conn.execute("SELECT dataset_id, emb FROM dgi_vec.item WHERE kind = 'dataset' AND emb IS NOT NULL").fetchall()
    if not rows:
        return {}
    m = np.asarray([r[1] for r in rows], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return {r[0]: m[i] for i, r in enumerate(rows)}


def scan(conn):
    conn.execute("TRUNCATE dgi_insight.nugget, dgi_insight.outlier")
    ind = {r[0]: r for r in conn.execute(
        "SELECT ind_id, matrix, dataset_id, label, level, is_count FROM dgi_insight.indicator")}
    cards = _cards(conn)
    if not cards:
        log.warning("no embeddings yet: nuggets are ranked by correlation only")
    for level in LEVELS:
        pop = _population(conn, level)
        data = defaultdict(lambda: defaultdict(dict))      # year -> ind -> area -> value
        for ind_id, year, area, value in conn.execute("""
                SELECT v.ind_id, v.year, v.area, v.value
                  FROM dgi_insight.ind_value v JOIN dgi_insight.indicator i USING (ind_id)
                 WHERE i.level = %s""", (level,)):
            data[year][ind_id][area] = value
        for year, inds in sorted(data.items()):
            areas_count = defaultdict(int)
            for vals in inds.values():
                for a in vals:
                    areas_count[a] += 1
            if len(areas_count) < MIN_AREAS:
                continue
            # The areas most indicators cover; keep indicators complete on them.
            need = max(areas_count.values()) * 0.9
            areas = sorted(a for a, c in areas_count.items() if c >= need)
            if len(areas) < MIN_AREAS:
                continue
            popv = _nearest(pop, year)
            pvec = (np.array([popv[a] for a in areas], dtype=np.float64)
                    if popv and all(popv.get(a) for a in areas) else None)
            prank = None
            if pvec is not None:
                prank = pvec.argsort().argsort().astype(np.float64)
                prank -= prank.mean()
                prank /= np.linalg.norm(prank)
            ids, rows, percap = [], [], []
            for ind_id, vals in inds.items():
                if not all(a in vals for a in areas):
                    continue
                x = np.array([vals[a] for a in areas], dtype=np.float64)
                if np.nanstd(x) == 0 or np.isnan(x).any():
                    continue
                pc = False
                if prank is not None and _is_count(ind[ind_id][3], x, prank):
                    x = x / pvec
                    pc = True
                ids.append(ind_id); rows.append(x); percap.append(pc)
            if len(ids) < 2:
                continue
            X = np.vstack(rows)
            # Spearman = Pearson on ranks.
            R = X.argsort(axis=1).argsort(axis=1).astype(np.float64)
            R -= R.mean(axis=1, keepdims=True)
            R /= np.linalg.norm(R, axis=1, keepdims=True)
            C_raw = R @ R.T
            # Partial: take out what each ranking shares with (1) population and
            # (2) the strongest pattern common to all indicators here, which is
            # area size and urbanness. Otherwise "big or urban areas have more
            # of everything" dominates, even when the population figure is a
            # little off (it is at council level: Cork/Galway city and county).
            basis = []
            if prank is not None:
                basis.append(prank)
            if len(ids) >= 5:
                basis.append(np.linalg.svd(R, full_matrices=False)[2][0])
            if basis:
                B = np.linalg.qr(np.vstack(basis).T)[0].T          # orthonormal rows
                P = R - (R @ B.T) @ B
                norms = np.linalg.norm(P, axis=1, keepdims=True)
                ok = norms[:, 0] > 0.05                # almost nothing left once size is removed
                P[ok] /= norms[ok]
                C = P @ P.T
                C[~ok, :] = 0
                C[:, ~ok] = 0
            else:
                C = C_raw
            iu = np.triu_indices(len(ids), k=1)
            rho = C[iu]
            keep = np.abs(rho) >= MIN_RHO
            cand = []
            for a, b, r in zip(iu[0][keep], iu[1][keep], rho[keep]):
                ia, ib = ind[ids[a]], ind[ids[b]]
                if ia[1] == ib[1]:                      # same CSO table: expected
                    continue
                ca, cb = cards.get(ia[2]), cards.get(ib[2])
                dist = float(1 - ca @ cb) if ca is not None and cb is not None else 0.5
                cand.append((level, year, ids[a], ids[b], float(r), len(areas), dist,
                             abs(float(r)) * dist, percap[a], percap[b], float(C_raw[a, b])))
            cand.sort(key=lambda c: -c[7])
            # Outliers: |z| > 3 within an indicator.
            Z = (X - X.mean(axis=1, keepdims=True)) / X.std(axis=1, keepdims=True)
            outl = [(level, year, areas[j], ids[i], float(Z[i, j]))
                    for i, j in zip(*np.where(np.abs(Z) > 3))]
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO dgi_insight.nugget (level, year, ind_a, ind_b, rho, n_areas, distance, "
                                "score, per_capita_a, per_capita_b, rho_raw) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                cand[:TOP_PER_LEVEL])
                cur.executemany("INSERT INTO dgi_insight.outlier VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                outl)
            conn.commit()
            log.info("%s %d: %d areas, %d indicators, %d strong pairs, %d outliers",
                     level, year, len(areas), len(ids), len(cand), len(outl))


def report(conn, top=40, per_table=2):
    date = time.strftime("%Y-%m-%d")
    path = Path.home() / "org" / f"{date.replace('-', '')}_dgi-nuggets.org"
    rows = conn.execute("""
        WITH best AS (
          SELECT DISTINCT ON (n.ind_a, n.ind_b)
                 n.*
               , count(*) OVER (PARTITION BY n.ind_a, n.ind_b) AS years_held
               , string_agg(n.year::text, ', ') OVER (PARTITION BY n.ind_a, n.ind_b) AS years
            FROM dgi_insight.nugget n
        ORDER BY n.ind_a, n.ind_b, n.score DESC
        )
        SELECT b.level, b.year, b.rho, b.n_areas, b.distance, b.score, x.label, y.label,
               x.dataset_id, y.dataset_id, b.per_capita_a, b.per_capita_b, b.rho_raw, b.years_held, b.years
          FROM best b
               JOIN dgi_insight.indicator x ON x.ind_id = b.ind_a
               JOIN dgi_insight.indicator y ON y.ind_id = b.ind_b
      ORDER BY b.score * (1 + 0.1 * least(b.years_held - 1, 5)) DESC
         LIMIT 20000""").fetchall()
    # Variety: one entry per pair of tables, and each table at most `per_table` times.
    seen_pairs, per = set(), defaultdict(int)
    picked = []
    for r in rows:
        ta, tb = r[6].split(" ")[0], r[7].split(" ")[0]
        pair = tuple(sorted((ta, tb)))
        if pair in seen_pairs or per[ta] >= per_table or per[tb] >= per_table:
            continue
        seen_pairs.add(pair)
        per[ta] += 1
        per[tb] += 1
        picked.append(r)
        if len(picked) >= top:
            break
    rows = picked
    lines = [f"#+TITLE: Nuggets from the data.gov.ie mirror ({date})", "#+STARTUP: overview", "",
             "Pairs of indicators that line up strongly across areas although they come",
             "from unrelated datasets. Ranked by |rho| x distance, where rho is the",
             "Spearman rank correlation across areas and distance is how different the",
             "two datasets' descriptions are (1 = unrelated).", "",
             "* Read these as leads, not findings",
             "- Correlation across areas is not causation; both may follow a third",
             "  thing (urban/rural, age profile, income).",
             "- What holds for areas need not hold for the people in them (the",
             "  ecological fallacy).",
             "- Size is held constant: rho is a partial rank correlation that removes",
             "  what both rankings share with population and with the strongest",
             "  pattern common to all indicators at that level (in practice area",
             "  size and urbanness), so 'big towns have more of everything' and the",
             "  urban/rural divide don't show up as nuggets. Counts were also",
             "  divided by population (marked per person); rates and averages were",
             "  compared as published.",
             "- A pattern that holds across several years is a stronger lead than",
             "  one year alone.",
             "- Check each one in the data before relying on it. The dataset pages",
             "  are at http://scierstar/dgi/d/<id>.html.", "",
             f"* Top {len(rows)}"]
    for i, (level, year, rho, n, dist, score, la, lb, da, dbb, pa, pb, rraw, held, years) in enumerate(rows, 1):
        lines += [f"** {i}. {la.split(':')[0][:60]} ~ {lb.split(':')[0][:60]}",
                  f"- {la}{' (per person)' if pa else ''}",
                  f"- {lb}{' (per person)' if pb else ''}",
                  f"- rho = {rho:+.2f} with population held constant (plain {rraw:+.2f}), across {n} areas "
                  f"({level}, best in {year}); distance {dist:.2f}",
                  f"- holds in {held} year(s): {years}" if held and held > 1 else f"- one year only: {year}",
                  f"- http://scierstar/dgi/d/{da}.html", f"- http://scierstar/dgi/d/{dbb}.html", ""]
    lines += ["* Queries", "#+begin_src sql",
              "-- All nuggets, best first",
              "  SELECT",
              "         n.level",
              "       , n.year",
              "       , round(n.rho::numeric, 2) AS rho",
              "       , n.n_areas",
              "       , a.label AS a",
              "       , b.label AS b",
              "    FROM dgi_insight.nugget n",
              "         JOIN dgi_insight.indicator a ON a.ind_id = n.ind_a",
              "         JOIN dgi_insight.indicator b ON b.ind_id = n.ind_b",
              "ORDER BY n.score DESC",
              "   LIMIT 100;", "#+end_src"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("report: %s", path)


def run(skip_indicators=False):
    with db.connect() as conn:
        if not skip_indicators:
            build_indicators(conn)
        scan(conn)
        report(conn)

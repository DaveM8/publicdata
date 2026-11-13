-- Phase 7: indicators lined up by place and year, for the nugget finder.
-- An indicator is one CSO statistic in one table, with every other breakdown
-- (sex, age, sector, ...) held at its total, giving one value per area per year.
-- Filled by `dgi-explore insights`.

-- Categories that mean "everything": State, Total, All ages, Both sexes, ...
CREATE OR REPLACE VIEW dgi_insight.total_cat AS
  SELECT cat_id
    FROM dgi_data.cso_cat
   WHERE code IN ('-', '_T', 'T', 'TOT', 'TOTAL', 'ALL')
      OR label ~* '^(all\M|total\M|both sexes|state$|any\M)';

CREATE TABLE IF NOT EXISTS dgi_insight.indicator (
    ind_id      int PRIMARY KEY,
    matrix_id   int NOT NULL,
    matrix      text NOT NULL,
    dataset_id  text,
    stat_cat    int,
    label       text NOT NULL,       -- "<table label>: <statistic>"
    unit        text,
    level       text NOT NULL,       -- county, la, lea, ed, nuts3, ...
    is_count    boolean              -- a count (divide by population before comparing areas)
);

CREATE TABLE IF NOT EXISTS dgi_insight.ind_value (
    ind_id  int NOT NULL,
    year    int NOT NULL,
    area    text NOT NULL,           -- dgi_ref.area code at the indicator's level
    value   double precision,
    PRIMARY KEY (ind_id, year, area)
);

CREATE TABLE IF NOT EXISTS dgi_insight.nugget (
    level      text NOT NULL,
    year       int NOT NULL,
    ind_a      int NOT NULL,
    ind_b      int NOT NULL,
    rho        real NOT NULL,        -- Spearman rank correlation across areas
    n_areas    int NOT NULL,
    distance   real,                 -- 1 - cosine similarity of the two datasets' cards
    score      real NOT NULL,        -- |rho| x distance: strong AND unexpected ranks high
    per_capita_a boolean,
    per_capita_b boolean,
    PRIMARY KEY (level, year, ind_a, ind_b)
);

CREATE TABLE IF NOT EXISTS dgi_insight.outlier (
    level    text NOT NULL,
    year     int NOT NULL,
    area     text NOT NULL,
    ind_id   int NOT NULL,
    z        real NOT NULL,          -- how many standard deviations from the other areas
    PRIMARY KEY (level, year, area, ind_id)
);

-- Added 13 Oct: rho is now the partial rank correlation with each area's
-- population held constant; rho_raw is the plain Spearman correlation.
ALTER TABLE dgi_insight.nugget ADD COLUMN IF NOT EXISTS rho_raw real;

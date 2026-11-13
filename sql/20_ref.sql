-- Reference areas at every level, with the names and codes they go by, for
-- recognising location columns in any dataset. Built from the boundaries
-- already in the gis database (census_raw.*, raw.ed_2022, staging.counties).
-- Rebuilt in full by `dgi-explore ref`.

-- Matching key: f_key() with all punctuation and spaces removed, so
-- 'DUN LAOGHAIRE/RATHDOWN', 'Dún Laoghaire-Rathdown' and 'dun laoghaire rathdown' agree.
CREATE OR REPLACE FUNCTION dgi_ref.akey(t text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(dgi_cat.f_key(t), '[^a-z0-9]+', '', 'g');

DROP TABLE IF EXISTS dgi_ref.alias, dgi_ref.area CASCADE;

CREATE TABLE dgi_ref.area (
    level        text NOT NULL,   -- state, province, nuts2, nuts3, county, la, lea, ed, sa
    code         text NOT NULL,
    name         text NOT NULL,
    name_ga      text,
    parent_level text,
    parent_code  text,
    geom         geometry(MultiPolygon, 2157),
    PRIMARY KEY (level, code)
);

CREATE TABLE dgi_ref.alias (
    level  text NOT NULL,
    code   text NOT NULL,
    akey   text NOT NULL,         -- dgi_ref.akey() of a name or code it goes by
    kind   text NOT NULL,         -- name, name_ga, code, variant
    FOREIGN KEY (level, code) REFERENCES dgi_ref.area
);

-- ── local authorities and their counties ─────────────────────────────────────
-- The EDs carry the 34 pre-2014 administrative county codes; merge them into
-- today's 31 local authorities. (Cork City's 2019 boundary extension isn't
-- reflected: the ED layer still uses the old city line.)
DROP TABLE IF EXISTS la_map, county_info;
CREATE TEMP TABLE la_map (ed_county_code text, la_code text, la_name text, la_name_ga text, county text);
INSERT INTO la_map VALUES
  ('01','CW','Carlow County Council','Comhairle Contae Cheatharlach','Carlow'),
  ('02','DC','Dublin City Council','Comhairle Cathrach Bhaile Átha Cliath','Dublin'),
  ('03','SD','South Dublin County Council','Comhairle Contae Átha Cliath Theas','Dublin'),
  ('04','FL','Fingal County Council','Comhairle Contae Fhine Gall','Dublin'),
  ('05','DR','Dún Laoghaire-Rathdown County Council','Comhairle Contae Dhún Laoghaire-Ráth an Dúin','Dublin'),
  ('06','KE','Kildare County Council','Comhairle Contae Chill Dara','Kildare'),
  ('07','KK','Kilkenny County Council','Comhairle Contae Chill Chainnigh','Kilkenny'),
  ('08','LS','Laois County Council','Comhairle Contae Laoise','Laois'),
  ('09','LD','Longford County Council','Comhairle Contae an Longfoirt','Longford'),
  ('10','LH','Louth County Council','Comhairle Contae Lú','Louth'),
  ('11','MH','Meath County Council','Comhairle Contae na Mí','Meath'),
  ('12','OY','Offaly County Council','Comhairle Contae Uíbh Fhailí','Offaly'),
  ('13','WH','Westmeath County Council','Comhairle Contae na hIarmhí','Westmeath'),
  ('14','WX','Wexford County Council','Comhairle Contae Loch Garman','Wexford'),
  ('15','WW','Wicklow County Council','Comhairle Contae Chill Mhantáin','Wicklow'),
  ('16','CE','Clare County Council','Comhairle Contae an Chláir','Clare'),
  ('17','CC','Cork City Council','Comhairle Cathrach Chorcaí','Cork'),
  ('18','CK','Cork County Council','Comhairle Contae Chorcaí','Cork'),
  ('19','KY','Kerry County Council','Comhairle Contae Chiarraí','Kerry'),
  ('20','LK','Limerick City and County Council','Comhairle Cathrach agus Contae Luimnigh','Limerick'),
  ('21','LK','Limerick City and County Council','Comhairle Cathrach agus Contae Luimnigh','Limerick'),
  ('22','TA','Tipperary County Council','Comhairle Contae Thiobraid Árann','Tipperary'),
  ('23','TA','Tipperary County Council','Comhairle Contae Thiobraid Árann','Tipperary'),
  ('24','WD','Waterford City and County Council','Comhairle Cathrach agus Contae Phort Láirge','Waterford'),
  ('25','WD','Waterford City and County Council','Comhairle Cathrach agus Contae Phort Láirge','Waterford'),
  ('26','GC','Galway City Council','Comhairle Cathrach na Gaillimhe','Galway'),
  ('27','GY','Galway County Council','Comhairle Contae na Gaillimhe','Galway'),
  ('28','LM','Leitrim County Council','Comhairle Contae Liatroma','Leitrim'),
  ('29','MO','Mayo County Council','Comhairle Contae Mhaigh Eo','Mayo'),
  ('30','RN','Roscommon County Council','Comhairle Contae Ros Comáin','Roscommon'),
  ('31','SO','Sligo County Council','Comhairle Contae Shligigh','Sligo'),
  ('32','CN','Cavan County Council','Comhairle Contae an Chabháin','Cavan'),
  ('33','DL','Donegal County Council','Comhairle Contae Dhún na nGall','Donegal'),
  ('34','MN','Monaghan County Council','Comhairle Contae Mhuineacháin','Monaghan');

CREATE TEMP TABLE county_info (county text, county_ga text, province text);
INSERT INTO county_info VALUES
  ('Carlow','Ceatharlach','Leinster'), ('Dublin','Baile Átha Cliath','Leinster'),
  ('Kildare','Cill Dara','Leinster'), ('Kilkenny','Cill Chainnigh','Leinster'),
  ('Laois','Laois','Leinster'), ('Longford','An Longfort','Leinster'), ('Louth','Lú','Leinster'),
  ('Meath','An Mhí','Leinster'), ('Offaly','Uíbh Fhailí','Leinster'),
  ('Westmeath','An Iarmhí','Leinster'), ('Wexford','Loch Garman','Leinster'),
  ('Wicklow','Cill Mhantáin','Leinster'), ('Clare','An Clár','Munster'), ('Cork','Corcaigh','Munster'),
  ('Kerry','Ciarraí','Munster'), ('Limerick','Luimneach','Munster'),
  ('Tipperary','Tiobraid Árann','Munster'), ('Waterford','Port Láirge','Munster'),
  ('Galway','Gaillimh','Connacht'), ('Leitrim','Liatroim','Connacht'), ('Mayo','Maigh Eo','Connacht'),
  ('Roscommon','Ros Comáin','Connacht'), ('Sligo','Sligeach','Connacht'),
  ('Cavan','An Cabhán','Ulster'), ('Donegal','Dún na nGall','Ulster'), ('Monaghan','Muineachán','Ulster');

-- ── areas, from the top down ─────────────────────────────────────────────────
INSERT INTO dgi_ref.area (level, code, name, name_ga, geom)
  SELECT
         'state'
       , 'IE'
       , 'Ireland'
       , 'Éire'
       , ST_Multi(ST_Union(geom))
    FROM staging.counties;

INSERT INTO dgi_ref.area (level, code, name, name_ga, parent_level, parent_code, geom)
  SELECT
         'province'
       , i.province
       , i.province
       , CASE i.province WHEN 'Leinster' THEN 'Laighin' WHEN 'Munster' THEN 'An Mhumhain'
                         WHEN 'Connacht' THEN 'Connachta' ELSE 'Ulaidh' END
       , 'state'
       , 'IE'
       , ST_Multi(ST_Union(c.geom))
    FROM staging.counties c
         JOIN county_info i ON i.county = c.name
GROUP BY i.province;

INSERT INTO dgi_ref.area (level, code, name, parent_level, parent_code, geom)
  SELECT
         'nuts2'
       , sa_nuts2
       , min(sa_nuts2_name)
       , 'state'
       , 'IE'
       , ST_Multi(ST_Union(geom))
    FROM census_raw.small_area
GROUP BY sa_nuts2;

INSERT INTO dgi_ref.area (level, code, name, parent_level, parent_code, geom)
  SELECT
         'nuts3'
       , sa_nuts3
       , min(sa_nuts3_name)
       , 'nuts2'
       , min(sa_nuts2)
       , ST_Multi(ST_Union(geom))
    FROM census_raw.small_area
GROUP BY sa_nuts3;

INSERT INTO dgi_ref.area (level, code, name, name_ga, parent_level, parent_code, geom)
  SELECT
         'county'
       , c.name
       , c.name
       , i.county_ga
       , 'province'
       , i.province
       , ST_Multi(c.geom)
    FROM staging.counties c
         JOIN county_info i ON i.county = c.name;

INSERT INTO dgi_ref.area (level, code, name, name_ga, parent_level, parent_code, geom)
  SELECT
         'la'
       , m.la_code
       , min(m.la_name)
       , min(m.la_name_ga)
       , 'county'
       , min(m.county)
       , ST_Multi(ST_Union(e.geom))
    FROM raw.ed_2022 e
         JOIN la_map m ON m.ed_county_code = e.county_code
GROUP BY m.la_code;

INSERT INTO dgi_ref.area (level, code, name, parent_level, parent_code, geom)
  SELECT
         'lea'
       , l.lea_id
       , l.lea_official
       , 'county'
       , initcap(l.county)
       , ST_Multi(l.geom)
    FROM census_raw.lea l;

INSERT INTO dgi_ref.area (level, code, name, name_ga, parent_level, parent_code, geom)
  -- Three EDs come in two pieces under one code; merge them.
  SELECT
         'ed'
       , e.ed_id_str
       , initcap(min(e.ed_english))
       , min(e.ed_gaeilge)
       , 'la'
       , min(m.la_code)
       , ST_Multi(ST_Union(e.geom))
    FROM raw.ed_2022 e
         LEFT JOIN la_map m ON m.ed_county_code = e.county_code
GROUP BY e.ed_id_str;

INSERT INTO dgi_ref.area (level, code, name, parent_level, parent_code, geom)
  SELECT
         'sa'
       , s.sa_pub2022
       , s.sa_pub2022
       , 'ed'
       , s.ed_id_str
       , ST_Multi(s.geom)
    FROM census_raw.small_area s;

CREATE INDEX area_geom ON dgi_ref.area USING gist (geom);

-- Councils cut into small pieces: overlap calculations against them are much
-- faster than against the full, detailed boundaries.
DROP TABLE IF EXISTS dgi_ref.la_sub;
CREATE TABLE dgi_ref.la_sub AS
  SELECT
         code
       , ST_Subdivide(geom, 256) AS geom
    FROM dgi_ref.area
   WHERE level = 'la';
CREATE INDEX la_sub_geom ON dgi_ref.la_sub USING gist (geom);

-- ── names and codes each area goes by ────────────────────────────────────────
INSERT INTO dgi_ref.alias
  SELECT level, code, dgi_ref.akey(name), 'name' FROM dgi_ref.area
   UNION
  SELECT level, code, dgi_ref.akey(name_ga), 'name_ga' FROM dgi_ref.area WHERE name_ga IS NOT NULL
   UNION
  SELECT level, code, dgi_ref.akey(code), 'code' FROM dgi_ref.area WHERE level NOT IN ('county', 'province')
   UNION
  -- 'Co. Cork', 'County Cork', 'Cork County', 'Contae Chorcaí' style variants
  SELECT 'county', code, dgi_ref.akey(v), 'variant'
    FROM dgi_ref.area,
         unnest(ARRAY['Co. ' || name, 'County ' || name, name || ' County', 'Co ' || name]) v
   WHERE level = 'county'
   UNION
  -- local authorities by short name: 'Fingal', 'Cork City', 'Galway County', 'DLR', ...
  SELECT 'la', code, dgi_ref.akey(v), 'variant'
    FROM dgi_ref.area,
         unnest(ARRAY[
           regexp_replace(name, ' (County|City|City and County) Council$', ''),
           regexp_replace(name, ' Council$', ''),
           regexp_replace(name, ' Council$', '') || ' Council'
         ]) v
   WHERE level = 'la'
   UNION
  SELECT 'la', 'DR', dgi_ref.akey(v), 'variant'
    FROM unnest(ARRAY['DLR', 'Dun Laoghaire Rathdown', 'Dún Laoghaire/Rathdown', 'Dun Laoghaire']) v
   UNION
  SELECT 'la', code, dgi_ref.akey(v), 'variant'
    FROM (VALUES ('DC', 'Dublin City'), ('CC', 'Cork City'), ('CK', 'Cork County'),
                 ('GC', 'Galway City'), ('GY', 'Galway County'), ('LK', 'Limerick'),
                 ('LK', 'Limerick City'), ('LK', 'Limerick County'), ('WD', 'Waterford'),
                 ('WD', 'Waterford City'), ('WD', 'Waterford County'), ('TA', 'Tipperary'),
                 ('TA', 'North Tipperary'), ('TA', 'South Tipperary'), ('SD', 'South Dublin'),
                 ('FL', 'Fingal')) AS x(code, v)
   UNION
  -- CSO geography labels
  SELECT 'state', 'IE', dgi_ref.akey(v), 'variant'
    FROM unnest(ARRAY['State', 'Ireland', 'Republic of Ireland', 'All Counties', 'Total']) v
   UNION
  SELECT 'province', 'Ulster', dgi_ref.akey('Ulster (part of)'), 'variant'
   UNION
  -- Codes the CSO and Tailte Éireann use for small areas, EDs and LEAs: the
  -- published codes of each census (2011/2016/2022, which mostly coincide),
  -- the GUIDs, and the same codes without leading zeros ('047161002' / '47161002').
  -- A 2011 or 2016 code matching here means the same-numbered 2022 area; the
  -- boundaries can differ slightly between censuses.
  SELECT 'sa', s.sa_pub2022, dgi_ref.akey(v), 'code'
    FROM census_raw.small_area s,
         unnest(ARRAY[s.sa_guid_2022, s.sa_guid_2016, s.sa_pub2016, s.sa_pub2011,
                      ltrim(s.sa_pub2022, '0'), ltrim(s.sa_pub2016, '0'), ltrim(s.sa_pub2011, '0'),
                      'A' || s.sa_pub2011]) v
   WHERE v IS NOT NULL
   UNION
  SELECT 'ed', e.ed_id_str, dgi_ref.akey(v), 'code'
    FROM raw.ed_2022 e,
         unnest(ARRAY[e.ed_guid, ltrim(e.ed_id_str, '0')]) v
   WHERE v IS NOT NULL
   UNION
  SELECT 'lea', l.lea_id, dgi_ref.akey(v), 'code'
    FROM census_raw.lea l,
         unnest(ARRAY[l.lea_guid, l.cso_lea]) v
   WHERE v IS NOT NULL;

CREATE INDEX alias_akey ON dgi_ref.alias (akey);

ANALYZE dgi_ref.area;
ANALYZE dgi_ref.alias;

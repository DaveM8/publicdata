# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

dgi-explore indexes, links and loads the data.gov.ie mirror (`/mnt/storage/datagovie_mirror/`) into Postgres, and serves it through a static site, a search page, Datasette and Metabase. The user guide, run times, current numbers and open to-dos are in `~/org/20261015_dgi-explore.org`. Read it before changing anything, and update it (checkboxes, numbers) when the state changes. Some code comments still point to the old name `~/org/dgi-explore.org`. That is the same file.

## Commands

```sh
uv run dgi-explore <step> [-n]          # every step can be re-run; -n = dry run where offered
uv run dgi-explore -v <step>            # debug logging
uv run pytest                           # all tests (tests/test_textio.py, tests/test_web.py)
uv run pytest tests/test_textio.py -k bom   # one test or a group of tests
bash deploy/refresh.sh                  # full refresh in order; run inside tmux session dgx
uv run dgi-explore ask-prepare          # rebuild the per-table CSO views (dgi_cso) and dgi_ask; run after load-cso
uv run dgi-explore web                  # the public Ask site on 127.0.0.1:8003 (DGI_WEB_PROVIDER / DGI_WEB_MODEL choose who answers)
uv run dgi-explore ask --steps "..."    # answer a question from the CSO tables (calls a model API, costs money; --provider deepseek|kimi|anthropic|openai)
```

The step order is catalog → profile → ref (once) → link → load-cso / load-csv / load-geo → embed → build-site / export-datasette → insights. Most steps only process new or changed files. link, build-site and export-datasette rebuild everything. Long runs go in tmux session `dgx`. The log is `~/.local/state/dgi-explore/dgi-explore.log`.

Some tests read real mirror files and are skipped if the mirror isn't mounted. They check exact Irish strings, so a change to textio must keep them passing.

## Architecture

- `dgi_explore/cli.py`: the argparse entry point. Each subcommand lazily imports one module and calls its `run()`/`build()`/`export()`.
- `dgi_explore/db.py`: the only place for locations: `DSN = "dbname=gis"`, `MIRROR`, `SQL_DIR`, `STATE_DIR`. `mirror_state()` opens the mirror's `state.sqlite` **read-only**. The mirror process owns it and may still be running, so never write to it.
- `sql/NN_*.sql`: DDL and SQL functions, one file per phase. The owning Python step applies them with `db.run_sql_files(conn, "30_link")` (the name without `.sql`). Each file has to stay safe to re-run (`IF NOT EXISTS`, `CREATE OR REPLACE`). Each schema belongs to one phase: `dgi_cat` (10), `dgi_prof` (15), `dgi_ref` (20), `dgi_link` (30/35), `dgi_data` (40 + load_csv), `dgi_geo` (load_geo), `dgi_vec` (50), `dgi_insight` (60), `dgi_ask` + `dgi_cso` (45, ask-prepare). `00_schemas.sql` creates every schema plus the shared `dgi_cat.f_key()` and `dgi_prof.text_fix`.
- `dgi_explore/textio.py`: **all text read from the mirror goes through this module** (`read_text`, `detect_encoding` + `iter_lines`, `clean`, `search_key`, `shapefile_encoding`, `Restorer`). The mirror mixes UTF-8, BOMs, Windows-1252, double-encoded UTF-8 and unlabelled shapefiles. Don't use `open(..., encoding=...)` or GDAL's encoding defaults on mirror files. Every repair is written to `dgi_prof.text_fix`, so none is silent. `textio.search_key()` (Python) and `dgi_cat.f_key()` (SQL) must apply the same rules. Loaded data tables keep the published text. `Restorer` guesses of lost letters (`�`) are only used for search and embedding text.
- Files are identified by sha256. A file published under several datasets is profiled and loaded once, and the incremental steps compare checksums with what is already in `dgi_prof`/the registries (`dgi_data.csv_table`, `dgi_geo.layer_table`).
- `link.py`: `STEPS` is a list of (name, table, SQL). A step that names a table truncates it first, and the steps after it add to that table. That's why `--step` alone only makes sense for "geo layers".
- `load_geo.py`/`profile.py` call `ogr2ogr` as a subprocess, with the encoding from `textio.shapefile_encoding`. Layers go to `dgi_geo.geo_*` in ITM (EPSG:2157) with a `geom` column. Zip members are extracted to `/mnt/pdf_workspace/dgx-tmp/`. The parallel steps use `multiprocessing.Pool`, and embed uses threads.
- `embed.py`: OpenAI `text-embedding-3-large` at 1024 dimensions, paced by `--tpm`. The key comes from `OPENAI_API_KEY` or `~/learnarr/.env`. Only items whose text changed are sent again. Run `embed -n` first to see the cost.
- `dgi_explore/ask/`: question answering (plan and decisions: `~/org/20261111_dgi-search-plan.org`). `prepare.py` builds one view per CSO table in `dgi_cso` (a named column per dimension, plus `value` and `unit`) and the index `dgi_ask.cso_info` (`sql/45_ask.sql`). `tools.py` holds the four tools the model calls (`search_tables`, `describe_table`, `find_place`, `run_sql`); `run_sql` is read-only with a timeout and a row limit. `llm.py` is the **only** module that talks to a model provider, so the model can be swapped by setting `DGI_ASK_PROVIDER`/`DGI_ASK_MODEL`; keep provider SDK calls out of every other file. DeepSeek is the default and Kimi is available (keys in `~/.config/dgi-explore/keys.env`); Claude and OpenAI are bring-your-own-key only. Model-written SQL runs as the restricted login `dgi_ask` when a `dgi_ask` line exists in `~/.pgpass`, otherwise as the normal login set read-only. `agent.py` is the loop and the system prompt, and logs to `~/.local/state/dgi-explore/ask.jsonl`.
- `dgi_explore/web/`: release 1 of the public site (`uv run dgi-explore web`, port 8003, schema `app` from `sql/70_app.sql`). Server-rendered Jinja2 pages, one stylesheet, one small script (`static/ask.js`) and nothing loaded from other sites; keep it that way (no CDNs, no trackers, no cookies). `app.py` holds the routes and settings (`DGI_WEB_*`), `runner.py` answers each question in a background thread and feeds the progress events, `store.py` is the `app.answer` table, `files.py` makes the Excel and CSV downloads, and **all page text lives in `messages.py`**. Tables on an answer page come from query rows via the model's `show_table` tool, never from model-typed text; model text is rendered with HTML disabled. A person's own API key is passed to the thread in memory only: never store or log it. `tests/test_web.py` runs without calling a model.
- Outputs: `site.py` writes to `/var/www/html/dgi/` from `templates/` (Jinja2). `datasette_export.py` writes `catalog.db` + `datasette-metadata.json` here. `search_app.py` is FastAPI on 127.0.0.1:8002 and holds every vector in memory, reloaded hourly. `insights.py` writes `~/org/<YYYYMMDD>_dgi-nuggets.org`, following the `~/org` naming rule. `deploy/` holds the sudo setup, the nginx snippet, the user systemd units and the Metabase script.

## Cautions

- martin (the vector-tile server) exits if any `dgi_geo` table has coordinates it can't reproject. On 29 Oct it went into a crash loop and filled `/` with syslog. load_geo now sets a geometry to NULL when a coordinate is beyond ±1e10. Keep that guard when changing load_geo.
- The `gis` database (~210 GB) is on the boot SSD, next to `/`. Check `df -h /` before big loads.
- SQL you write (in `sql/` or in the org docs) follows the river style in `~/CLAUDE.md`.

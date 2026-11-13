"""dgi-explore: index, link and explore the data.gov.ie mirror.

Every step is safe to re-run. Steps that change the database accept
-n/--dry-run to show what they would do. Guide: ~/org/20261015_dgi-explore.org.
"""

import argparse
import logging
import logging.handlers
import sys

from . import db


def setup_logging(verbose):
    db.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("dgi")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logfile = logging.handlers.RotatingFileHandler(db.STATE_DIR / "dgi-explore.log",
                                                   maxBytes=20 << 20, backupCount=5)
    logfile.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(logfile)


def cmd_catalog(args):
    from . import catalog
    catalog.run(dry_run=args.dry_run)


def cmd_profile(args):
    from . import profile
    profile.run(dry_run=args.dry_run, workers=args.workers, limit=args.limit, redo=args.redo,
                kinds=args.kind, sha=args.sha, failed=args.failed)


def cmd_link(args):
    from . import link
    link.run(dry_run=args.dry_run, step=args.step)


def cmd_ref(args):
    from . import db
    with db.connect() as conn:
        db.run_sql_files(conn, "20_ref")


def cmd_load_cso(args):
    from . import load_cso
    load_cso.run(dry_run=args.dry_run, workers=args.workers, limit=args.limit)


def cmd_load_geo(args):
    from . import load_geo
    load_geo.run(dry_run=args.dry_run, workers=args.workers, limit=args.limit, redo=args.redo,
                 failed=args.failed)


def cmd_load_csv(args):
    from . import load_csv
    load_csv.run(dry_run=args.dry_run, workers=args.workers, limit=args.limit, redo=args.redo)


def cmd_embed(args):
    from . import embed
    embed.run(dry_run=args.dry_run, prepare_only=args.prepare_only, threads=args.threads, limit=args.limit,
              tpm=args.tpm)


def cmd_build_site(args):
    from . import site
    site.build(dry_run=args.dry_run)


def cmd_export_datasette(args):
    from . import datasette_export
    datasette_export.export()


def cmd_insights(args):
    from . import insights
    insights.run(skip_indicators=args.skip_indicators)


def cmd_dataset_views(args):
    from . import load_csv
    load_csv.dataset_views()


def cmd_ask_prepare(args):
    from .ask import prepare
    prepare.run()


def cmd_ask(args):
    from .ask import agent
    agent.cli(" ".join(args.question), show_steps=args.steps, provider=args.provider,
              model=args.model, effort=args.effort)


def main(argv=None):
    p = argparse.ArgumentParser(prog="dgi-explore", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("catalog", help="load publishers, datasets and resources into dgi_cat")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_catalog)

    s = sub.add_parser("profile", help="profile every downloaded file into dgi_prof (incremental)")
    s.add_argument("-n", "--dry-run", action="store_true", help="only count the files to do")
    s.add_argument("-j", "--workers", type=int, default=12)
    s.add_argument("--limit", type=int, help="profile at most this many files (smallest first)")
    s.add_argument("--redo", action="store_true", help="profile again even if already done")
    s.add_argument("--kind", action="append", help="with --redo: only files of this kind (repeatable)")
    s.add_argument("--sha", help="just this file (sha256)")
    s.add_argument("--failed", action="store_true", help="profile again only the files that had errors")
    s.set_defaults(func=cmd_profile)

    s = sub.add_parser("ref", help="rebuild the reference areas and their names (dgi_ref)")
    s.set_defaults(func=cmd_ref)

    s = sub.add_parser("link", help="find location and time keys; rebuild dgi_link")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("--step", help='only the steps whose name contains this, e.g. "geo layers"')
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("load-cso", help="load CSO JSON-stat tables into dgi_data (incremental)")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("-j", "--workers", type=int, default=10)
    s.add_argument("--limit", type=int, help="load at most this many tables (for a trial)")
    s.set_defaults(func=cmd_load_cso)

    s = sub.add_parser("load-geo", help="load map layers into PostGIS schema dgi_geo (incremental)")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("-j", "--workers", type=int, default=6)
    s.add_argument("--limit", type=int, help="load at most this many layers (smallest first)")
    s.add_argument("--redo", action="store_true", help="load again even if already loaded")
    s.add_argument("--failed", action="store_true", help="try again the layers that failed")
    s.set_defaults(func=cmd_load_geo)

    s = sub.add_parser("load-csv", help="load tidy CSV/TSV files as tables in dgi_data (incremental)")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.add_argument("-j", "--workers", type=int, default=10)
    s.add_argument("--limit", type=int, help="load at most this many files (smallest first)")
    s.add_argument("--redo", action="store_true")
    s.set_defaults(func=cmd_load_csv)

    s = sub.add_parser("embed", help="build cards and document chunks, and embed them with OpenAI (incremental)")
    s.add_argument("-n", "--dry-run", action="store_true", help="build the items and show the cost; embed nothing")
    s.add_argument("--prepare-only", action="store_true", help="same as -n")
    s.add_argument("--threads", type=int, default=4)
    s.add_argument("--limit", type=int, help="embed at most this many items")
    s.add_argument("--tpm", type=int, default=900_000, help="tokens per minute to stay under (account limit)")
    s.set_defaults(func=cmd_embed)

    s = sub.add_parser("build-site", help="write the static browser to /var/www/html/dgi/")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_build_site)

    s = sub.add_parser("export-datasette", help="write catalog.db for Datasette")
    s.set_defaults(func=cmd_export_datasette)

    s = sub.add_parser("insights", help="find strong, unexpected patterns between datasets; write the nuggets report")
    s.add_argument("--skip-indicators", action="store_true", help="reuse the indicators from the last run")
    s.set_defaults(func=cmd_insights)

    s = sub.add_parser("dataset-views", help="rebuild the per-dataset views dgi_data.ds_<id8>")
    s.set_defaults(func=cmd_dataset_views)

    s = sub.add_parser("ask-prepare", help="build the per-table CSO views (dgi_cso) and the ask index (dgi_ask)")
    s.set_defaults(func=cmd_ask_prepare)

    s = sub.add_parser("ask", help="answer a question in plain words from the CSO tables")
    s.add_argument("question", nargs="+")
    s.add_argument("--steps", action="store_true", help="show each tool call and its result")
    s.add_argument("--provider", help="deepseek (default), kimi, anthropic or openai; or DGI_ASK_PROVIDER")
    s.add_argument("--model", help="model id (default: DGI_ASK_MODEL, or the provider's usual model)")
    s.add_argument("--effort", help="low, medium, high, xhigh or max (default: DGI_ASK_EFFORT or high)")
    s.set_defaults(func=cmd_ask)

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()

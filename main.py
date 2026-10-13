#!/usr/bin/env python3

import sys
import json
import time
import argparse
import logging

from cache import FileCache
from rules import build_rules, resolve
from formatters import TSV_HEADER, format_tsv, format_human


def _add_cache_args(parser):
    parser.add_argument(
        "--cache-file", metavar="PATH",
        help="Persist result cache to a JSON file",
    )
    parser.add_argument(
        "--cache-ttl", type=float, default=24.0, metavar="HOURS",
        help="Cache expiry in hours (default: 24, 0 = never expire)",
    )


def _add_verbose_arg(parser):
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Log which tool handled each lookup (routing, cache, fallbacks) to stderr",
    )


def _setup_logging(verbose):
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="[verbose] %(message)s", stream=sys.stderr)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def _make_cache(args):
    ttl = None if args.cache_ttl == 0 else args.cache_ttl * 3600
    cache = FileCache(args.cache_file) if args.cache_file else None
    return cache, ttl


def run_cli(args):
    _setup_logging(args.verbose)
    cache, ttl = _make_cache(args)
    rules = build_rules(cache, ttl)
    delay = 0.0 if args.no_delay else args.delay
    first = True

    if args.format == "tsv":
        print(TSV_HEADER)

    try:
        for line in sys.stdin:
            token = line.strip()
            if not token or token.startswith("#"):
                continue
            if not first and delay > 0:
                time.sleep(delay)
            first = False

            search_fn, query = resolve(token, rules)
            result, error = search_fn(query)

            # search_url returns a list; search returns a single dict.
            results = result if isinstance(result, list) else ([result] if result else [])

            if error:
                display_url = query
                if args.format == "json":
                    print(json.dumps({"status": "error", "url": display_url, "error": error}, ensure_ascii=False))
                elif args.format == "tsv":
                    print(format_tsv(None, error, display_url))
                else:
                    print(format_human(None, error, token))
            else:
                for r in results:
                    if args.format == "json":
                        print(json.dumps({"status": "ok", **r}, ensure_ascii=False))
                    elif args.format == "tsv":
                        print(format_tsv(r, None, r["url"]))
                    else:
                        print(format_human(r, None, token))

            sys.stdout.flush()
    except KeyboardInterrupt:
        sys.exit(0)


def run_serve(args):
    try:
        import uvicorn
        from server import create_app
    except ImportError:
        print(
            "Server dependencies missing. Install with:\n"
            "  pip install 'part-finder[server]'",
            file=sys.stderr,
        )
        sys.exit(1)

    _setup_logging(args.verbose)
    cache, ttl = _make_cache(args)
    app = create_app(cache=cache, ttl=ttl)
    uvicorn.run(app, host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(
        description="Look up robotics parts by SKU or URL.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── pipe (default) ──────────────────────────────
    pipe = subparsers.add_parser("pipe", help="Read SKUs/URLs from stdin (default)")
    pipe.add_argument("--format", choices=["human", "json", "tsv"], default="human")
    pipe.add_argument("--delay", type=float, default=1.0, metavar="SECONDS")
    pipe.add_argument("--no-delay", action="store_true")
    _add_cache_args(pipe)
    _add_verbose_arg(pipe)

    # ── serve ───────────────────────────────────────
    serve = subparsers.add_parser("serve", help="Start HTTP API server")
    serve.add_argument("--host", default="0.0.0.0", metavar="HOST")
    serve.add_argument("--port", type=int, default=8000, metavar="PORT")
    _add_cache_args(serve)
    _add_verbose_arg(serve)

    # Default to pipe mode when no subcommand is given, so that
    # `part-finder --no-delay` still works without typing "pipe".
    if len(sys.argv) == 1 or sys.argv[1] not in ("pipe", "serve", "-h", "--help"):
        sys.argv.insert(1, "pipe")

    args = parser.parse_args()

    if args.command == "serve":
        run_serve(args)
    else:
        run_cli(args)


if __name__ == "__main__":
    main()

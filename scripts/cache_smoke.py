#!/usr/bin/env python3
"""Smoke-test the two cache layers against a real `serve` process.

Starts `python main.py serve -v` with a short result TTL, then queries every SKU three times:

  1. cold    -> must hit the network (search or direct fetch)
  2. repeat  -> must be a result-cache hit (no outbound request)
  3. expired -> result cache has expired; tools with a URL map must skip the search

Each phase is classified from the server's own -v log lines, so this checks which
path ran, not just that a response came back. Needs network access to the stores.

  python scripts/cache_smoke.py                 # default SKU set
  python scripts/cache_smoke.py WCP-1838 am-3645
  python scripts/cache_smoke.py --slow          # also an AndyMark SKU that forces the catalog crawl
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# sku -> (vendor, does the tool keep a SKU->URL map?)
SKUS = {
    "WCP-1838":       ("WCP",       False),  # handle == SKU: direct .js fetch, nothing to map
    "WCP-1810":       ("WCP",       False),
    "am-0255_15T":    ("AndyMark",  True),
    "am-14U5_NGB":    ("AndyMark",  True),
    "am-3645":        ("AndyMark",  True),
    "TTB-0001":       ("ThriftyBot", True),
    # REV, single products: the page is /<sku>/, fetched directly, nothing to map
    "REV-11-2159":    ("REV",       False),
    "REV-21-1652":    ("REV",       False),
    "REV-11-2158":    ("REV",       False),  # page also has an add-on option: must not be mistaken for a variant
    "REV-11-1228":    ("REV",       False),  # page SKU is REV-11-1228-PK10: matched by the -PKn fallback
    # REV, variant SKUs: /<sku>/ 404s, so search, then the option API; the URL map skips the search
    "REV-21-2108":    ("REV",       True),   # option label ends with the SKU: "Kit (REV-21-2108)"
    "REV-21-2809":    ("REV",       True),   # option label starts with the SKU: "(REV-21-2809) ..."
    "5000-0002-0001": ("goBILDA",   True),
    "1310-0016-4008": ("goBILDA",   True),
}
SLOW = {"am-5484_BN2CMA": ("AndyMark (catalog fallback)", False)}  # not in Storefront search

MARKERS = [  # first match wins; order matters
    ("result-cache", "cache hit for"),
    ("url-map",      "URL map hit for"),
    ("search",       "Storefront search hit for"),
    ("search",       "search page hit for"),
    ("direct",       "direct page hit for"),
    ("catalog",      "catalog hit for"),
    ("direct",       ".js handle hit for"),
    ("direct",       "JSON-LD hit for"),
]


def classify(log: str) -> str:
    for label, needle in MARKERS:
        if needle in log:
            return label
    return "unknown"


def query(port: int, sku: str) -> list:
    url = f"http://127.0.0.1:{port}/api/v1/lookup?" + urllib.parse.urlencode({"q": sku})
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skus", nargs="*")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ttl", type=float, default=3.0, help="result-cache TTL in seconds")
    ap.add_argument("--slow", action="store_true", help="include a SKU that triggers the catalog crawl")
    args = ap.parse_args()

    table = dict(SKUS)
    if args.slow:
        table.update(SLOW)
    if args.skus:
        table = {s: table.get(s, ("?", False)) for s in args.skus}

    err = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    server = subprocess.Popen(
        [sys.executable, "main.py", "serve", "-v", "--port", str(args.port),
         "--cache-ttl", str(args.ttl / 3600)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=err,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{args.port}/docs", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            sys.exit("server did not start; see " + err.name)

        def run_phase(sku):
            start = os.path.getsize(err.name)
            t0 = time.time()
            body = query(args.port, sku)
            ms = (time.time() - t0) * 1000
            with open(err.name) as f:
                f.seek(start)
                log = f.read()
            return classify(log), ms, body

        rows, failures = [], 0
        for sku, (vendor, has_map) in table.items():
            cold, cold_ms, body = run_phase(sku)
            repeat, repeat_ms, _ = run_phase(sku)
            time.sleep(args.ttl + 1)
            expired, expired_ms, _ = run_phase(sku)

            got = body[0].get("sku", "").upper() if body else ""
            # A multi-pack page answers with <sku>-PKn (the tools' -PKn fallback).
            ok_body = bool(body) and body[0].get("status") == "ok" and (
                got == sku.upper() or got.startswith(sku.upper() + "-PK"))
            want_expired = "url-map" if has_map else None
            problems = []
            if not ok_body:
                problems.append(f"no exact result ({body[0].get('error') if body else 'empty'})")
            if repeat != "result-cache":
                problems.append(f"repeat was {repeat}, wanted result-cache")
            if want_expired and expired != want_expired:
                problems.append(f"after TTL was {expired}, wanted {want_expired}")
            if not want_expired and expired in ("result-cache", "url-map"):
                problems.append(f"after TTL was {expired}, expected a fresh fetch")
            failures += bool(problems)
            rows.append((sku, vendor, cold, f"{cold_ms:.0f}", repeat, f"{repeat_ms:.0f}",
                         expired, f"{expired_ms:.0f}", "ok" if not problems else "; ".join(problems)))

        head = ("SKU", "vendor", "cold", "ms", "repeat", "ms", "after TTL", "ms", "verdict")
        widths = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head))]
        for r in [head] + rows:
            print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
        print(f"\n{len(rows) - failures}/{len(rows)} passed   (server log: {err.name})")
        sys.exit(1 if failures else 0)
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()

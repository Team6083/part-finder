# part-finder

Robotics parts price and stock lookup — CLI and HTTP API.
Supports WCP, AndyMark, REV Robotics, ThriftyBot, and goBILDA.

## Setup

```bash
git clone https://github.com/yourname/part-finder
cd part-finder
pip install -r requirements.txt
```

---

## CLI

Reads one SKU or URL per line from stdin.

```bash
echo "WCP-1838" | python main.py

printf "WCP-1838\nam-0255_15T\nREV-11-2159\n" | python main.py

# Mix of SKUs and direct URLs
printf "WCP-1838\nhttps://www.gobilda.com/1310-series-hyper-hub-8mm-rex-bore/\n" | python main.py

# Lines starting with # are ignored
cat parts.txt | python main.py
```

### Output formats

```bash
python main.py --format human   # default — readable block per item
python main.py --format json    # one JSON object per line
python main.py --format tsv     # TSV with header row
```

### Request delay

```bash
python main.py --delay 0.5   # 0.5s between requests (default: 1s)
python main.py --no-delay
```

### Verbose mode

```bash
python main.py -v          # also: python main.py serve -v
```

Logs to stderr which tool handled each lookup: the matched rule, cache hit/miss,
and any fallback (e.g. Storefront search → catalog). stdout is unchanged, so
`--format json` output stays machine-readable.

### Caching

```bash
# Cache to a file: results for 10 minutes, SKU -> product URL for 7 days
python main.py --cache-file cache.json

# Custom TTLs (hours), 0 = never expire
python main.py --cache-file cache.json --cache-ttl 1 --map-ttl 24
python main.py --cache-file cache.json --cache-ttl 0
```

There are two cache layers over the same backend:

| Layer | Holds | Default TTL | Flag |
|---|---|---|---|
| Result cache | full lookup result (price, stock) | 10 minutes | `--cache-ttl` |
| URL map | SKU → product page URL (AndyMark, ThriftyBot, goBILDA, and the WCP fallback) | 7 days | `--map-ttl` |

When the result cache expires, a URL map hit goes straight to the product page and
skips the search. If that page 404s or no longer has the SKU, the entry is dropped
and the search runs again. `serve` caches in memory by default (per process, bounded, least recently used evicted);
pass `--no-cache` to turn caching off. A `--cache-file` is safe for one process at a time:
two processes sharing a file can overwrite each other's entries.

For AndyMark and ThriftyBot, lookups use Storefront API search. Only when that finds
no exact SKU match does the first query fall back to fetching the full product
catalog (kept in memory for the rest of the session).

### Checking the cache against live stores

`scripts/cache_smoke.py` starts `serve -v`, queries each SKU three times (cold, immediately
again, after the result TTL expires) and reports from the server log which path served each one:
a fresh fetch, a result-cache hit, or a URL-map hit. It calls the real store sites, so it is a manual
check and is not run by CI or included in the Docker image.

```bash
python scripts/cache_smoke.py                  # default SKUs across all stores
python scripts/cache_smoke.py WCP-1810 am-3645 # only these
python scripts/cache_smoke.py --slow           # also an AndyMark SKU that forces the catalog crawl
```

---

## HTTP server

Requires `fastapi` and `uvicorn`:

```bash
pip install fastapi "uvicorn[standard]"
```

```bash
python main.py serve
python main.py serve --host 127.0.0.1 --port 8080 --cache-file cache.json
```

### Endpoints

**Single lookup**
```
GET /lookup?q=WCP-1838
GET /lookup?q=https://www.revrobotics.com/rev-11-2159/
```

**Batch lookup**
```
POST /lookup
Content-Type: application/json

{"queries": ["WCP-1838", "am-0255_15T", "REV-11-2159"]}
```

**Interactive docs** — `http://localhost:8000/docs`

### Hardening for public deployment

| Setting | Effect |
|---|---|
| `PART_FINDER_API_KEY` | If set, every `/lookup` call must send `X-API-Key: <value>` (constant-time compare), else 401. Unset = no auth. |
| `PART_FINDER_MAX_BATCH` | Max queries per `POST /lookup` (default 50); more returns 422. |
| `PART_FINDER_CACHE_MAX_ENTRIES` | Max entries in the in-memory cache, evicted least recently used (default 10000). |

Queries are limited to 2048 characters. Only the store hosts from `rules.py` are fetched
(redirects are followed manually, at most 5 hops, each checked against that list) and responses are
capped at 5 MB. Error messages to clients are fixed strings (`not found`, `unsupported input`,
`upstream error`); details go to the server log. The Docker image runs as a non-root user.
`/docs` is not protected by the API key.

### Response

```json
{
  "status": "ok",
  "name": "IP10 Torx Plus Bits (50mm Length, 2-Pack)",
  "sku": "WCP-1838",
  "price": "9.99",
  "in_stock": true,
  "stock_status": "in_stock",
  "source": "shopify_rest",
  "url": "https://wcproducts.com/products/wcp-1838",
  "error": ""
}
```

---

## Supported stores

| SKU format | Store | Method |
|---|---|---|
| `WCP-XXXX` | [West Coast Products](https://wcproducts.com) | Shopify REST (SKU = handle) |
| `am-XXXX` / `am-XXXX_VAR` | [AndyMark](https://www.andymark.com) | Shopify Storefront search, catalog fallback |
| `REV-XX-XXXX` | [REV Robotics](https://www.revrobotics.com) | JSON-LD (SKU = URL handle) |
| `TTB-XXXX` | [ThriftyBot](https://www.thethriftybot.com) | Shopify Storefront search, catalog fallback |
| `XXXX-XXXX-XXXX` | [goBILDA](https://www.gobilda.com) | BigCommerce search + JSON-LD |

Direct URLs from any supported store are also accepted.
On the CLI, unknown URLs fall back to auto-detection (Shopify REST or JSON-LD). `serve` never
fetches hosts that no rule names: unknown URLs and unmatched inputs return an error.

---

## Adding a new store

Edit `rules.py` and add a `Rule` to `build_rules()`.

```python
# Shopify — SKU is the URL handle (like WCP)
Rule(w(ShopifyHandleTool("https://example.com")),
     sku(r"^ex-\d+$"), domain("example.com"))

# Shopify — SKU is not the handle, needs catalog scan (like AndyMark)
Rule(w(ShopifyCatalogTool("https://example.com")),
     sku(r"^ex-\d+"), domain("example.com"))

# BigCommerce — search-based (like goBILDA)
Rule(w(BigCommerceSearchTool("https://example.com")),
     sku(r"^\d{4}-\d{4}$"), domain("example.com"))
```

A `Rule` accepts any number of `sku(pattern)` (regex) and `domain(host, ...)` conditions.
`domain()` takes plain hostnames and matches a URL whose hostname is exactly that host or a
subdomain of it (`example.com` matches `www.example.com`, not `notexample.com` or
`evil.com/?x=example.com`). In `serve` mode these hosts are also the only hosts the server will
fetch, including redirect targets, so a new store needs no other allowlist change.
When a cache is active (`--cache-file`, or always for `serve` unless `--no-cache`), all rules are
automatically wrapped with `CachedTool`.

---

## Project structure

```
part-finder/
├── main.py         # CLI entry point + serve subcommand
├── server.py       # FastAPI app
├── parsers.py      # HTTP fetch, Shopify REST, JSON-LD
├── cache.py        # ResultCache, MemoryCache, FileCache
├── tools.py        # SearchTool and all store implementations
├── rules.py        # sku(), domain(), Rule, build_rules, resolve
├── formatters.py   # format_human, format_tsv
└── requirements.txt
```

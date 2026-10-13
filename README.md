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

### Caching

```bash
# Cache results with default 24h TTL
python main.py --cache-file cache.json

# Custom TTL (hours), 0 = never expire
python main.py --cache-file cache.json --cache-ttl 1
python main.py --cache-file cache.json --cache-ttl 0
```

For AndyMark and ThriftyBot, the first query fetches the full product catalog to
build a SKU→handle mapping. All subsequent queries in the same session read
directly from memory.

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
| `am-XXXX` / `am-XXXX_VAR` | [AndyMark](https://www.andymark.com) | Shopify catalog (paginated) |
| `REV-XX-XXXX` | [REV Robotics](https://www.revrobotics.com) | JSON-LD (SKU = URL handle) |
| `TTB-XXXX` | [ThriftyBot](https://www.thethriftybot.com) | Shopify catalog (paginated) |
| `XXXX-XXXX-XXXX` | [goBILDA](https://www.gobilda.com) | BigCommerce search + JSON-LD |

Direct URLs from any supported store are also accepted.
Unknown URLs fall back to auto-detection (Shopify REST or JSON-LD).

---

## Adding a new store

Edit `rules.py` and add a `Rule` to `build_rules()`.

```python
# Shopify — SKU is the URL handle (like WCP)
Rule(w(ShopifyHandleTool("https://example.com")),
     sku(r"^ex-\d+$"), domain(r"example\.com"))

# Shopify — SKU is not the handle, needs catalog scan (like AndyMark)
Rule(w(ShopifyCatalogTool("https://example.com")),
     sku(r"^ex-\d+"), domain(r"example\.com"))

# BigCommerce — search-based (like goBILDA)
Rule(w(BigCommerceSearchTool("https://example.com")),
     sku(r"^\d{4}-\d{4}$"), domain(r"example\.com"))
```

A `Rule` accepts any number of `sku(pattern)` and `domain(pattern)` conditions.
When `--cache-file` is set, all rules are automatically wrapped with `CachedTool`.

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

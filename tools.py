import requests
from abc import ABC, abstractmethod
from bs4 import BeautifulSoup

from cache import ResultCache
from parsers import HEADERS, _fetch, parse_shopify_rest, parse_json_ld


class SearchTool(ABC):
    @abstractmethod
    def search(self, query: str) -> tuple:
        """Search by SKU. Return (result_dict | None, error_str | None)."""

    def search_url(self, url: str) -> tuple:
        """Search by URL. Return (list[dict] | None, error_str | None)."""
        try:
            r = _fetch(url)
            return parse_json_ld(url, r.text), None
        except Exception as e:
            return None, str(e)


class CachedTool(SearchTool):
    """Decorator: wraps any SearchTool with a result cache and optional TTL."""

    def __init__(self, tool: SearchTool, cache: ResultCache, ttl: float | None = None):
        self._tool = tool
        self._cache = cache
        self._ttl = ttl

    def _cached(self, key, fn):
        hit = self._cache.get(key, self._ttl)
        if hit is not None:
            return hit, None
        result, error = fn()
        if result:
            self._cache.set(key, result)
        return result, error

    def search(self, query):
        return self._cached(query, lambda: self._tool.search(query))

    def search_url(self, url):
        return self._cached(url, lambda: self._tool.search_url(url))


class GenericUrlTool(SearchTool):
    """Fallback for unknown URLs: auto-detect platform."""

    def search(self, query):
        return self.search_url(query)

    def search_url(self, url):
        try:
            r = _fetch(url)
        except Exception as e:
            return None, str(e)
        html = r.text
        try:
            if "cdn.shopify.com" in html or "window.Shopify" in html:
                return parse_shopify_rest(url), None
            return parse_json_ld(url, html), None
        except Exception as e:
            return None, str(e)


class ShopifyHandleTool(SearchTool):
    """
    Shopify stores where the product handle == SKU (lowercased).
    Falls back to /search.json for stores that expose it.
    Example: WCP  →  /products/wcp-1838
    """

    def __init__(self, store_base_url: str):
        self.base = store_base_url

    def search(self, sku):
        try:
            variants = parse_shopify_rest(f"{self.base}/products/{sku.lower()}")
            match = next((v for v in variants if v["sku"].upper() == sku.upper()), None)
            if match:
                return match, None
        except Exception:
            pass

        try:
            r = requests.get(
                f"{self.base}/search.json?q={sku}&type=product",
                headers=HEADERS, timeout=10,
            )
            for hit in (r.json().get("results", []) if r.status_code == 200 else []):
                path = hit.get("url", "").split("?")[0]
                if not path:
                    continue
                try:
                    variants = parse_shopify_rest(self.base + path)
                    match = next((v for v in variants if v["sku"].upper() == sku.upper()), None)
                    if match:
                        return match, None
                except Exception:
                    continue
        except Exception:
            pass

        return None, f"SKU {sku} not found on {self.base}"

    def search_url(self, url):
        try:
            return parse_shopify_rest(url), None
        except Exception as e:
            return None, str(e)


class ShopifyCatalogTool(SearchTool):
    """
    Shopify stores where handle != SKU.
    Paginates /products.json once to build a full SKU→result catalog,
    then serves queries directly from memory.
    Example: AndyMark, ThriftyBot
    """

    def __init__(self, store_base_url: str):
        self.base = store_base_url
        self._catalog: dict | None = None          # sku_upper → result dict
        self._by_handle: dict | None = None        # handle → list[result dict]

    def _build(self):
        catalog = {}
        by_handle = {}
        page = 1
        while True:
            try:
                r = requests.get(
                    f"{self.base}/products.json?limit=250&page={page}",
                    headers=HEADERS, timeout=15,
                )
                products = r.json().get("products", [])
            except Exception:
                break
            if not products:
                break
            for p in products:
                url = f"{self.base}/products/{p['handle']}"
                handle_results = []
                for v in p.get("variants", []):
                    s = v.get("sku", "")
                    if not s:
                        continue
                    variant_title = v.get("title", "")
                    name = p["title"] if not variant_title or variant_title == "Default Title" \
                           else f"{p['title']} – {variant_title}"
                    available = v.get("available", False)
                    entry = {
                        "name":         name,
                        "sku":          s,
                        "price":        str(v.get("price", "")),
                        "in_stock":     available,
                        "stock_status": "in_stock" if available else "out_of_stock",
                        "source":       "shopify_catalog",
                        "url":          url,
                    }
                    catalog[s.upper()] = entry
                    handle_results.append(entry)
                if handle_results:
                    by_handle[p["handle"]] = handle_results
            page += 1
        self._catalog = catalog
        self._by_handle = by_handle

    def _ensure(self):
        if self._catalog is None:
            self._build()

    def search(self, sku):
        self._ensure()
        result = self._catalog.get(sku.upper())
        if not result:
            return None, f"SKU {sku} not found in catalog of {self.base}"
        return result, None

    def search_url(self, url):
        self._ensure()
        # Extract handle from URL path: /products/{handle}
        path = url.rstrip("/").rsplit("/", 1)[-1]
        results = self._by_handle.get(path)
        if results:
            return results, None
        # Catalog might not be built yet or handle not found; fall back to JSON-LD
        try:
            r = _fetch(url)
            return parse_json_ld(url, r.text), None
        except Exception as e:
            return None, str(e)


class JsonLdHandleTool(SearchTool):
    """
    Stores where the product URL is predictable from the SKU
    and data comes from JSON-LD on the page.
    Example: REV Robotics  →  /rev-11-2159/
    """

    def __init__(self, store_base_url: str, url_fn=None):
        self.base = store_base_url
        self._url_fn = url_fn or (lambda sku: f"{store_base_url}/{sku.lower()}/")

    def search(self, sku):
        url = self._url_fn(sku)
        try:
            r = _fetch(url)
            results = parse_json_ld(url, r.text)
            match = next((v for v in results if v["sku"].upper() == sku.upper()), results[0])
            return match, None
        except Exception as e:
            return None, str(e)


class BigCommerceSearchTool(SearchTool):
    """
    BigCommerce stores where SKU != URL handle.
    Searches via /search.php, takes the first result, parses JSON-LD.
    Example: goBILDA
    """

    def __init__(self, store_base_url: str):
        self.base = store_base_url

    def search(self, sku):
        try:
            r = _fetch(f"{self.base}/search.php?search_query={sku}")
        except Exception as e:
            return None, str(e)

        card = BeautifulSoup(r.text, "html.parser").select_one("li.product a.card")
        if not card:
            return None, f"SKU {sku} not found on {self.base}"

        product_url = card["href"].split("?")[0]
        try:
            r2 = _fetch(product_url)
            results = parse_json_ld(product_url, r2.text)
            match = next((v for v in results if v["sku"].upper() == sku.upper()), None)
            if not match:
                return None, f"SKU mismatch: {sku} not found in search results"
            return match, None
        except Exception as e:
            return None, str(e)

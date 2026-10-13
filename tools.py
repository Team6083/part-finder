import logging
import os
import time
import requests
from abc import ABC, abstractmethod
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from cache import ResultCache
from parsers import (
    HEADERS, _fetch, parse_shopify_rest, parse_json_ld,
    format_price, variant_name,
)

log = logging.getLogger(__name__)

STOREFRONT_API_VERSION = os.environ.get("SHOPIFY_STOREFRONT_API_VERSION", "2026-07")


class SearchTool(ABC):
    @property
    def name(self) -> str:
        base = getattr(self, "base", "")
        host = urlparse(base).netloc if base else ""
        return f"{type(self).__name__}({host})" if host else type(self).__name__

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

    @property
    def name(self) -> str:
        return f"Cached[{self._tool.name}]"

    def _cached(self, key, fn):
        hit = self._cache.get(key, self._ttl)
        if hit is not None:
            log.debug("%s: cache hit for %s", self.name, key)
            return hit, None
        log.debug("%s: cache miss for %s", self.name, key)
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
        if "cdn.shopify.com" in html or "window.Shopify" in html:
            try:
                results = parse_shopify_rest(url)
                log.debug("%s: Shopify .js hit for %s", self.name, url)
                return results, None
            except Exception as e:
                log.debug("%s: Shopify .js failed for %s (%s); trying JSON-LD", self.name, url, e)
        try:
            results = parse_json_ld(url, html)
            log.debug("%s: JSON-LD hit for %s", self.name, url)
            return results, None
        except Exception as e:
            return None, str(e)


class ShopifyHandleTool(SearchTool):
    """
    Shopify stores where the product handle == SKU (lowercased).
    Tries /products/{handle}.js first; falls back to ``fallback`` (e.g. a
    ShopifyStorefrontTool) when the handle lookup misses.
    Example: WCP  →  /products/wcp-1838
    """

    def __init__(self, store_base_url: str, fallback: SearchTool | None = None):
        self.base = store_base_url
        self._fallback = fallback

    def search(self, sku):
        try:
            variants = parse_shopify_rest(f"{self.base}/products/{sku.lower()}")
            match = next((v for v in variants if v["sku"].upper() == sku.upper()), None)
            if match:
                log.debug("%s: .js handle hit for %s", self.name, sku)
                return match, None
            log.debug("%s: .js handle has no exact SKU match for %s", self.name, sku)
        except Exception as e:
            log.debug("%s: .js handle lookup failed for %s (%s)", self.name, sku, e)

        if self._fallback:
            log.debug("%s: falling back to %s", self.name, self._fallback.name)
            return self._fallback.search(sku)
        return None, f"SKU {sku} not found on {self.base}"

    def search_url(self, url):
        try:
            results = parse_shopify_rest(url)
            log.debug("%s: .js hit for %s", self.name, url)
            return results, None
        except Exception as e:
            return None, str(e)


class ShopifyStorefrontTool(SearchTool):
    """
    Shopify stores queried through the unauthenticated Storefront GraphQL API.
    SKU lookups use full-text ``search`` and require an exact SKU match on a
    variant (never the first hit). URL lookups use the product's .js endpoint.
    Any API failure or miss is delegated to ``fallback`` when provided.
    Example: AndyMark, ThriftyBot
    """

    SEARCH_QUERY = """
    query ($q: String!) {
      search(first: 5, query: $q, types: PRODUCT) {
        nodes {
          ... on Product {
            handle
            title
            variants(first: 100) {
              pageInfo { hasNextPage endCursor }
              nodes { sku title availableForSale price { amount } }
            }
          }
        }
      }
    }
    """

    VARIANTS_QUERY = """
    query ($handle: String!, $after: String) {
      product(handle: $handle) {
        variants(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes { sku title availableForSale price { amount } }
        }
      }
    }
    """

    def __init__(self, store_base_url: str, fallback: SearchTool | None = None,
                 api_version: str = STOREFRONT_API_VERSION):
        self.base = store_base_url
        self._fallback = fallback
        self._endpoint = f"{store_base_url}/api/{api_version}/graphql.json"

    def _graphql(self, query: str, variables: dict) -> dict:
        r = requests.post(
            self._endpoint,
            json={"query": query, "variables": variables},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise ValueError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    @staticmethod
    def _find_variant(nodes, sku):
        return next((v for v in nodes if (v.get("sku") or "").upper() == sku.upper()), None)

    def _to_result(self, product: dict, v: dict) -> dict:
        available = bool(v.get("availableForSale"))
        return {
            "name":         variant_name(product["title"], v.get("title", "")),
            "sku":          v["sku"],
            "price":        format_price(v["price"]["amount"]),
            "in_stock":     available,
            "stock_status": "in_stock" if available else "out_of_stock",
            "source":       "shopify_storefront",
            "url":          f"{self.base}/products/{product['handle']}",
        }

    def _lookup(self, sku):
        """Return a result dict or None. Raises on API failure."""
        data = self._graphql(self.SEARCH_QUERY, {"q": sku})
        products = [p for p in data["search"]["nodes"] if p]
        for p in products:
            variants = p["variants"]
            match = self._find_variant(variants["nodes"], sku)
            if match:
                return self._to_result(p, match)
        # No match on the first page of any hit: walk further variant pages.
        for p in products:
            variants = p["variants"]
            while variants["pageInfo"]["hasNextPage"]:
                log.warning("%s has more than 100 variants; paginating", p["handle"])
                d = self._graphql(self.VARIANTS_QUERY, {
                    "handle": p["handle"],
                    "after": variants["pageInfo"]["endCursor"],
                })
                variants = d["product"]["variants"]
                match = self._find_variant(variants["nodes"], sku)
                if match:
                    return self._to_result(p, match)
        return None

    def search(self, sku):
        try:
            result = self._lookup(sku)
            if result:
                log.debug("%s: Storefront search hit for %s", self.name, sku)
                return result, None
            reason = f"SKU {sku} not found on {self.base}"
            log.debug("%s: Storefront search has no exact SKU match for %s", self.name, sku)
        except Exception as e:
            log.warning("Storefront search failed for %s: %s", sku, e)
            reason = str(e)
        if self._fallback:
            log.debug("%s: falling back to %s", self.name, self._fallback.name)
            return self._fallback.search(sku)
        return None, reason

    def search_url(self, url):
        try:
            results = parse_shopify_rest(url)
            log.debug("%s: .js hit for %s", self.name, url)
            return results, None
        except Exception as e:
            log.debug("%s: .js failed for %s (%s)", self.name, url, e)
            if self._fallback:
                log.debug("%s: falling back to %s", self.name, self._fallback.name)
                return self._fallback.search_url(url)
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

    PAGE_DELAY = 0.4
    MAX_ATTEMPTS = 3

    def _fetch_page(self, page):
        """Fetch one catalog page, retrying 429 / 5xx / non-JSON with exponential backoff."""
        last_error = None
        for attempt in range(self.MAX_ATTEMPTS):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                r = requests.get(
                    f"{self.base}/products.json?limit=250&page={page}",
                    headers=HEADERS, timeout=15,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                return r.json().get("products", [])
            except (requests.RequestException, ValueError) as e:
                last_error = str(e)
        raise RuntimeError(
            f"Catalog page {page} of {self.base} failed after "
            f"{self.MAX_ATTEMPTS} attempts: {last_error}"
        )

    def _build(self):
        """Build the full catalog. Raises on failure so a partial catalog is never stored."""
        catalog = {}
        by_handle = {}
        page = 1
        while True:
            products = self._fetch_page(page)
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
            time.sleep(self.PAGE_DELAY)
        self._catalog = catalog
        self._by_handle = by_handle
        log.debug("%s: built catalog with %d SKUs", self.name, len(catalog))

    def _ensure(self):
        if self._catalog is None:
            self._build()

    def search(self, sku):
        try:
            self._ensure()
        except Exception as e:
            return None, str(e)
        result = self._catalog.get(sku.upper())
        if not result:
            return None, f"SKU {sku} not found in catalog of {self.base}"
        log.debug("%s: catalog hit for %s", self.name, sku)
        return result, None

    def search_url(self, url):
        # Extract handle from URL path: /products/{handle}
        path = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        try:
            self._ensure()
            results = self._by_handle.get(path)
            if results:
                log.debug("%s: catalog hit for %s", self.name, path)
                return results, None
        except Exception as e:
            log.debug("%s: catalog unavailable (%s); trying JSON-LD", self.name, e)
        # Catalog might not be built yet or handle not found; fall back to JSON-LD
        try:
            r = _fetch(url)
            results = parse_json_ld(url, r.text)
            log.debug("%s: JSON-LD hit for %s", self.name, url)
            return results, None
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
            log.debug("%s: JSON-LD hit for %s at %s", self.name, sku, url)
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
        log.debug("%s: search page first hit %s", self.name, product_url)
        try:
            r2 = _fetch(product_url)
            results = parse_json_ld(product_url, r2.text)
            match = next((v for v in results if v["sku"].upper() == sku.upper()), None)
            if not match:
                return None, f"SKU mismatch: {sku} not found in search results"
            log.debug("%s: JSON-LD hit for %s", self.name, sku)
            return match, None
        except Exception as e:
            return None, str(e)

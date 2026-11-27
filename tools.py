import json
import logging
import os
import re
import threading
import time
import requests
from abc import ABC, abstractmethod
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup

from cache import ResultCache, UrlMap
from parsers import (
    HEADERS, MAX_BODY_BYTES, FetchError, _check_url, NoProductError, _fetch, read_capped, safe_get, parse_shopify_rest, parse_json_ld,
    format_price, variant_name,
)

log = logging.getLogger(__name__)

STOREFRONT_API_VERSION = os.environ.get("SHOPIFY_STOREFRONT_API_VERSION", "2026-07")


def mapped_lookup(url_map: UrlMap | None, base: str, sku: str, fetch_match, name: str):
    """Serve a SKU from its mapped product URL, skipping the search step.

    ``fetch_match(url)`` returns the matching variant dict or None (page loads but has
    no such SKU). The entry is dropped only when the product is really gone: a
    404/410, a page with no product data, or a page without the SKU. Timeouts,
    429s and 5xx keep it, since those say nothing about the mapping. Returns the
    variant, or None to search.
    """
    url = url_map.get(base, sku) if url_map else None
    if not url:
        return None
    try:
        match = fetch_match(url)
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status not in (404, 410):
            log.debug("%s: mapped URL %s transient failure for %s (%s); keeping entry", name, url, sku, e)
            return None
    except NoProductError:
        pass
    except Exception as e:
        log.debug("%s: mapped URL %s transient failure for %s (%s); keeping entry", name, url, sku, e)
        return None
    else:
        if match:
            log.debug("%s: URL map hit for %s -> %s", name, sku, url)
            return match
    log.debug("%s: dropping stale URL map entry for %s", name, sku)
    url_map.delete(base, sku)
    return None


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


def normalize_url(url: str) -> str:
    """Cache key for a URL: scheme, lowercase host and path only (no userinfo, query or fragment)."""
    p = urlparse(url)
    return urlunparse((p.scheme.lower(), p.netloc.rpartition("@")[2].lower(), p.path, "", "", ""))


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
        try:
            hit = self._cache.get(key, self._ttl)
        except Exception as e:
            log.warning("%s: cache read failed (%s); treating as a miss", self.name, e)
            hit = None
        if hit is not None:
            log.debug("%s: cache hit for %s", self.name, key)
            return hit, None
        log.debug("%s: cache miss for %s", self.name, key)
        result, error = fn()
        if result:
            try:
                self._cache.set(key, result)
            except Exception as e:
                log.warning("%s: cache write failed (%s); continuing", self.name, e)
        return result, error

    def search(self, query):
        return self._cached(query, lambda: self._tool.search(query))

    def search_url(self, url):
        u = normalize_url(url)
        return self._cached(u, lambda: self._tool.search_url(u))


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
                 api_version: str = STOREFRONT_API_VERSION, url_map: UrlMap | None = None):
        self.base = store_base_url
        self._fallback = fallback
        self._url_map = url_map
        self._endpoint = f"{store_base_url}/api/{api_version}/graphql.json"

    def _graphql(self, query: str, variables: dict) -> dict:
        r = requests.post(
            self._endpoint,
            json={"query": query, "variables": variables},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=10,
            allow_redirects=False,
            stream=True,
        )
        r.raise_for_status()
        read_capped(r, MAX_BODY_BYTES)
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

    def _from_mapped_url(self, sku):
        def fetch_match(url):
            return next((v for v in parse_shopify_rest(url)
                         if v["sku"].upper() == sku.upper()), None)
        match = mapped_lookup(self._url_map, self.base, sku, fetch_match, self.name)
        # Same source as a fresh search, so output doesn't change when the result cache expires.
        return {**match, "source": "shopify_storefront"} if match else None

    def search(self, sku):
        mapped = self._from_mapped_url(sku)
        if mapped:
            return mapped, None
        try:
            result = self._lookup(sku)
            if result:
                log.debug("%s: Storefront search hit for %s", self.name, sku)
                if self._url_map:
                    self._url_map.set(self.base, sku, result["url"])
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
        self._lock = threading.Lock()
        self._failed_at: float | None = None
        self._failure = ""

    PAGE_DELAY = 0.4
    MAX_ATTEMPTS = 3
    MAX_PAGE_BYTES = 25 * 1024 * 1024
    COOLDOWN = 300.0                               # seconds before retrying a failed build

    def _fetch_page(self, page):
        """Fetch one catalog page, retrying 429 / 5xx / non-JSON with exponential backoff."""
        last_error = None
        for attempt in range(self.MAX_ATTEMPTS):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                r = safe_get(
                    f"{self.base}/products.json?limit=250&page={page}",
                    max_bytes=self.MAX_PAGE_BYTES, timeout=15,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                return r.json().get("products", [])
            except FetchError:
                raise
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
        self._by_handle = by_handle
        self._catalog = catalog
        log.debug("%s: built catalog with %d SKUs", self.name, len(catalog))

    def _ensure(self):
        """Build the catalog in one thread; concurrent callers fail fast instead of waiting.
        A failed build is not retried until COOLDOWN has passed (callers get the same error)."""
        if self._catalog is not None:
            return
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("catalog build in progress")
        try:
            if self._catalog is not None:
                return
            if self._failed_at is not None and time.monotonic() - self._failed_at < self.COOLDOWN:
                raise RuntimeError(f"catalog build failed recently; not retrying yet ({self._failure})")
            try:
                self._build()
            except Exception as e:
                self._failed_at = time.monotonic()
                self._failure = str(e)
                raise
            self._failed_at = None
        finally:
            self._lock.release()

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
    Searches via /search.php, takes the first results, parses JSON-LD.
    Pages whose JSON-LD only has a price range (variants chosen via product
    options, e.g. REV Robotics) are resolved through the product-attributes API.
    Example: goBILDA, REV Robotics
    """

    MAX_SEARCH_CANDIDATES = 3
    MAX_VARIANTS = 24

    def __init__(self, store_base_url: str, url_map: UrlMap | None = None, url_fn=None):
        self.base = store_base_url
        self._url_map = url_map
        # Optional SKU -> product URL guess, tried before searching (REV single products: /rev-11-2159/).
        self._url_fn = url_fn

    @staticmethod
    def _options(html):
        """(product_id, name, [(attribute_id, value_id, label)]) for option-based variants."""
        soup = BeautifulSoup(html, "html.parser")
        pid = soup.select_one("input[name=product_id]")
        h1 = soup.select_one("h1")
        options = []
        for label in soup.select("label[data-product-attribute-value]"):
            m = re.match(r"attribute_\w+?_(\d+)_(\d+)$", label.get("for", ""))
            if m:
                options.append((m[1], m[2], label.get_text(" ", strip=True)))
        for sel in soup.select("select[name^='attribute[']"):
            attr = re.search(r"\[(\d+)\]", sel["name"])
            for opt in sel.select("option[value]"):
                if attr and opt["value"]:
                    options.append((attr[1], opt["value"], opt.get_text(" ", strip=True)))
        return (pid.get("value") if pid else None,
                h1.get_text(strip=True) if h1 else "", options)

    @staticmethod
    def _label_sku(label):
        """SKU in parentheses anywhere in an option label: "Kit (REV-21-2108)" or "(REV-21-2809) Shaft"."""
        m = re.search(r"\(([A-Za-z]{2,}-\d[\w-]*)\)", label)
        return m[1] if m else None

    @staticmethod
    def _page_attributes(html):
        """The page's ``BCData.product_attributes`` (sku, upc, ... of the default variant), or {}."""
        m = re.search(r"var\s+BCData\s*=\s*", html)
        if not m:
            return {}
        try:
            data, _ = json.JSONDecoder().raw_decode(html, m.end())
        except ValueError:
            return {}
        attrs = data.get("product_attributes") if isinstance(data, dict) else None
        return attrs if isinstance(attrs, dict) else {}

    def _variant(self, url, pid, name, attr, value, label, upc=None):
        endpoint = f"{self.base}/remote/v1/product-attributes/{pid}"
        _check_url(endpoint)
        r = requests.post(endpoint, headers=HEADERS, timeout=10, allow_redirects=False, stream=True,
                          data={"action": "add", "product_id": pid, "qty[]": 1,
                                f"attribute[{attr}]": value})
        if 300 <= r.status_code < 400:
            r.close()
            raise FetchError(f"unexpected redirect from {endpoint}")
        r.raise_for_status()
        read_capped(r, MAX_BODY_BYTES)
        data = r.json().get("data")
        # An unknown or partial selection comes back 200 with sku None (or data []): not a variant.
        label_sku = self._label_sku(label)
        if not isinstance(data, dict) or not data.get("sku") or \
                (label_sku and data["sku"].upper() != label_sku.upper()):
            return None
        if upc is not None and not same_upc(data.get("upc"), upc):
            return None
        price = ((data.get("price") or {}).get("without_tax") or {}).get("value", "")
        in_stock = bool(data.get("instock")) and data.get("purchasable", True) is not False
        return {
            "name":         f"{name} - {label}" if name else label,
            "sku":          data["sku"],
            "price":        format_price(price) if price != "" else "",
            "in_stock":     in_stock,
            "stock_status": "in_stock" if in_stock else "out_of_stock",
            "source":       "bigcommerce_variant",
            "url":          url,
        }

    def _variants(self, url, html, sku=None, upc=None, deadline=None):
        """Variants of an option-based product page; only the one matching sku or upc if given.

        Only options whose label carries a SKU are queried. With a sku, errors propagate so the
        caller can tell a transient failure from a missing SKU; when listing a page, a failing
        option is skipped. A upc queries the options in turn and stops at the first whose UPC
        matches; a failing option is skipped, and the error is raised only if nothing matched.
        Past ``deadline`` (time.monotonic()) no further option is queried: UpcBudgetExceeded.
        """
        pid, name, options = self._options(html)
        if not pid or not pid.isdigit():
            return []
        labelled = [(a, v, l, self._label_sku(l)) for a, v, l in options]
        labelled = [o for o in labelled if o[3]]
        if sku:
            want = sku.upper()
            exact = [o for o in labelled if o[3].upper() == want]
            # Multi-packs are listed as REV-21-2003-PK10; fall back to them only when no exact match.
            labelled = exact or [o for o in labelled if o[3].upper().startswith(want + "-PK")]
        if len(labelled) > self.MAX_VARIANTS:
            log.warning("%s: %s lists %d variants; showing the first %d",
                        self.name, url, len(labelled), self.MAX_VARIANTS)
            labelled = labelled[:self.MAX_VARIANTS]
        out, failure = [], None
        for attr, value, label, _ in labelled:
            check_deadline(deadline)
            try:
                variant = self._variant(url, pid, name, attr, value, label, upc)
            except Exception as e:
                if sku:
                    raise
                failure = e
                log.debug("%s: skipping option %s on %s (%s)", self.name, label, url, e)
                continue
            if variant:
                out.append(variant)
                if upc:
                    break
        if upc and not out and failure:
            raise failure
        return out

    def _product_by_upc(self, product_url, upc, deadline=None):
        """Fetch a product page and return the variant whose UPC is upc, or None."""
        check_deadline(deadline)
        r = _fetch(product_url)
        try:
            results = parse_json_ld(product_url, r.text)
        except NoProductError:
            results = []
        attrs = self._page_attributes(r.text)
        page_match = same_upc(attrs.get("upc"), upc)
        if page_match and attrs.get("sku"):
            page_sku = str(attrs["sku"]).upper()
            match = next((v for v in results if str(v["sku"]).upper() == page_sku), None)
            if match:
                return match
        # Option-based page: each variant has its own UPC, only reachable per option.
        variants = self._variants(product_url, r.text, upc=upc, deadline=deadline)
        if variants:
            return variants[0]
        # The page's UPC matched but its SKU isn't in the JSON-LD. Trust the single JSON-LD
        # entry only if it is a real SKU or the page has no options; otherwise it is the base
        # product (REV option pages list the numeric product id), not the barcode's variant.
        if page_match and len(results) == 1:
            only_sku = str(results[0]["sku"])
            if (only_sku and not only_sku.isdigit()) or not self._options(r.text)[2]:
                return results[0]
        if not results and not attrs:
            raise NoProductError("No JSON-LD Product found")
        return None

    def _product(self, product_url, sku):
        """Fetch a product page and return the variant matching sku, or None."""
        r = _fetch(product_url)
        try:
            results = parse_json_ld(product_url, r.text)
        except NoProductError:
            results = []
        match = next((v for v in results if str(v["sku"]).upper() == sku.upper()), None)
        if match:
            return match
        variants = self._variants(product_url, r.text, sku)
        if variants:
            return variants[0]
        # Multi-packs are listed as REV-11-1228-PK10; same fallback as for option labels.
        pack = next((v for v in results if str(v["sku"]).upper().startswith(sku.upper() + "-PK")), None)
        if pack:
            return pack
        if not results:
            raise NoProductError("No JSON-LD Product found")
        return None

    def search_url(self, url):
        try:
            r = _fetch(url)
            try:
                results = parse_json_ld(url, r.text)
            except NoProductError:
                results = []
            _, _, options = self._options(r.text)
            label_skus = {(self._label_sku(l) or "").upper() for _, _, l in options} - {""}
            # No real SKU in the JSON-LD (BigCommerce's bare product id, or none): the variants are
            # only reachable per option. A real SKU that is itself one of the options means the
            # JSON-LD only shows the default variant. A real SKU the options don't list means the
            # options are add-ons (e.g. REV-11-2158's data port cover), not variants of this page.
            if options and all(not str(v["sku"]) or str(v["sku"]).isdigit()
                               or str(v["sku"]).upper() in label_skus for v in results):
                variants = self._variants(url, r.text)
                if variants:
                    return variants, None
            if not results:
                raise NoProductError("No JSON-LD Product found")
            return results, None
        except Exception as e:
            return None, str(e)

    def search(self, sku):
        direct = self._url_fn(sku) if self._url_fn else None
        return self._find(sku, "SKU", sku, lambda url: self._product(url, sku), direct)

    def search_upc(self, upc, deadline=None):
        """Look up a UPC (as normalized by normalize_upc) via the store search.

        ``deadline`` (time.monotonic()) stops further requests once passed."""
        try:
            check_deadline(deadline)
        except UpcBudgetExceeded as e:
            return None, str(e)
        return self._find(upc, "UPC", f"UPC:{upc}",
                          lambda url: self._product_by_upc(url, upc, deadline), deadline=deadline)

    def _find(self, query, kind, map_key, product_match, direct_url=None, deadline=None):
        """URL map, then the direct page guess, then the store search for ``query``.

        ``product_match(url)`` returns the matching variant on a product page or None.
        ``kind`` ("SKU" / "UPC") names the query in error messages; ``map_key`` keys the URL map entry.
        """
        mapped = mapped_lookup(self._url_map, self.base, map_key, product_match, self.name)
        if mapped:
            return mapped, None

        if direct_url:
            try:
                match = product_match(direct_url)
                if match:
                    log.debug("%s: direct page hit for %s", self.name, query)
                    return match, None
            except Exception as e:
                log.debug("%s: no direct page for %s (%s); searching", self.name, query, e)

        try:
            check_deadline(deadline)
            r = _fetch(f"{self.base}/search.php?search_query={query}")
        except Exception as e:
            return None, str(e)

        # goBILDA links the card itself; REV links the figure inside it.
        cards = BeautifulSoup(r.text, "html.parser").select("li.product a.card, li.product .card-figure a[href]")
        if not cards:
            return None, f"{kind} {query} not found on {self.base}"

        seen = []
        for card in cards:
            product_url = card["href"].split("?")[0]
            if product_url not in seen:
                seen.append(product_url)
        err, checked = None, False
        for product_url in seen[:self.MAX_SEARCH_CANDIDATES]:
            log.debug("%s: search page candidate %s", self.name, product_url)
            try:
                match = product_match(product_url)
            except UpcBudgetExceeded as e:
                return None, str(e)
            except Exception as e:
                err = str(e)
                continue
            checked = True
            if match:
                log.debug("%s: search page hit for %s", self.name, query)
                if self._url_map:
                    self._url_map.set(self.base, map_key, product_url)
                return match, None
        return None, (None if checked else err) or f"{kind} mismatch: {query} not found in search results"


class UpcBudgetExceeded(RuntimeError):
    """A UPC lookup used up its time budget before finding a match."""


def check_deadline(deadline) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise UpcBudgetExceeded("UPC lookup time budget exceeded")


def normalize_upc(code: str) -> str:
    """Digits of a UPC-A / EAN-13 / GTIN-14, with zero padding beyond 12 digits removed.

    "0722787604379" and "00722787604379" both become "722787604379", the form stores search by.
    """
    digits = re.sub(r"\D", "", code)
    while len(digits) > 12 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def same_upc(a, b) -> bool:
    """Whether two barcodes are the same GTIN, ignoring leading zero padding."""
    if not a or not b:
        return False
    a, b = normalize_upc(str(a)), normalize_upc(str(b))
    return bool(a) and a == b


class UpcTool(SearchTool):
    """UPC / EAN barcode lookup across stores that expose one (``search_upc``).

    Stores are tried in order and the first exact UPC match wins. A miss everywhere
    reports "not found"; if any store failed (timeout, 5xx) that error is reported
    instead, since the product may be on that store.

    One lookup may probe several candidate pages and every option on them, so it gets a
    time budget (``budget`` seconds, env PART_FINDER_UPC_BUDGET, default 30). Once it is
    spent no new request starts; a request already in flight still runs to its own timeout.
    """

    DEFAULT_BUDGET = 30.0

    def __init__(self, stores, budget: float | None = None):
        self._stores = list(stores)
        if budget is None:
            budget = float(os.environ.get("PART_FINDER_UPC_BUDGET", self.DEFAULT_BUDGET))
        self._budget = budget

    @property
    def name(self) -> str:
        return f"UpcTool[{', '.join(s.name for s in self._stores)}]"

    def search(self, code):
        upc = normalize_upc(code)
        deadline = time.monotonic() + self._budget
        failures = []
        for store in self._stores:
            result, err = store.search_upc(upc, deadline)
            if result:
                log.debug("%s: UPC %s found by %s", self.name, upc, store.name)
                return result, None
            low = (err or "").lower()
            if err and "not found" not in low and "mismatch" not in low and "no json-ld" not in low:
                failures.append(f"{store.name}: {err}")
        if failures:
            return None, "; ".join(failures)
        return None, f"UPC {upc} not found"

    def search_url(self, url):
        return None, "UPC lookup does not take URLs"

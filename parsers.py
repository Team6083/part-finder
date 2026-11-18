import json
import requests
from decimal import Decimal, InvalidOperation
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from urllib3.util import parse_url

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


class NoProductError(ValueError):
    """The page loaded but holds no product data (e.g. a removed product's redirect page)."""


MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)

_allowed_hosts: frozenset[str] | None = None


class FetchError(ValueError):
    """A request was refused (disallowed host or scheme, too many redirects, oversized body)."""


def set_allowed_hosts(hosts) -> None:
    """Restrict every fetch (and each redirect hop) to these hosts and their subdomains.
    None lifts the restriction (CLI with generic URL fallback)."""
    global _allowed_hosts
    _allowed_hosts = None if hosts is None else frozenset(h.lower() for h in hosts)


def host_matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def url_host(url: str) -> str | None:
    """Lowercased hostname of an http(s) URL, or None. Uses the parser requests connects with."""
    try:
        parsed = parse_url(url)
    except Exception:
        return None
    if (parsed.scheme or "").lower() not in ("http", "https") or not parsed.host:
        return None
    return parsed.host.lower()


def _check_url(url: str) -> None:
    host = url_host(url)
    if host is None:
        raise FetchError("unsupported URL")
    if _allowed_hosts is not None and not host_matches(host, _allowed_hosts):
        raise FetchError(f"host not allowed: {host}")


def read_capped(r, max_bytes: int) -> None:
    length = r.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > max_bytes:
        r.close()
        raise FetchError("response too large")
    body = bytearray()
    for chunk in r.iter_content(65536):
        body += chunk
        if len(body) > max_bytes:
            r.close()
            raise FetchError("response too large")
    r._content = bytes(body)


def safe_get(url, max_bytes=MAX_BODY_BYTES, timeout=10):
    """GET with manual redirects (each hop host-checked) and a response size cap. No status check."""
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(url)
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=False, stream=True)
        if r.status_code in _REDIRECT_CODES:
            location = r.headers.get("Location")
            r.close()
            if not location:
                raise FetchError("redirect without Location")
            url = urljoin(url, location)
            continue
        read_capped(r, max_bytes)
        return r
    raise FetchError("too many redirects")


def _fetch(url, **kwargs):
    r = safe_get(url, **kwargs)
    r.raise_for_status()
    return r


def format_price(amount) -> str:
    """Normalize a decimal amount (str/int/Decimal) to a 2-decimal string."""
    try:
        return str(Decimal(str(amount)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return str(amount)


def format_cents(cents) -> str:
    """Convert integer cents (Shopify .js prices) to a 2-decimal string, e.g. 999 -> '9.99'."""
    try:
        return str((Decimal(int(cents)) / 100).quantize(Decimal("0.01")))
    except (TypeError, ValueError):
        return ""


def variant_name(title: str, variant_title: str) -> str:
    if not variant_title or variant_title == "Default Title":
        return title
    return f"{title} – {variant_title}"


def parse_shopify_rest(url) -> list[dict]:
    """Fetch all variants of a Shopify product via the public /products/{handle}.js endpoint.

    Unlike the .json endpoint, .js includes per-variant ``available``; its prices are
    integer cents and the response root is the product itself.
    """
    parsed = urlparse(url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    for suffix in (".json", ".js"):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break
    product = _fetch(f"{clean}.js").json()
    variants = product.get("variants", [])
    if not variants:
        raise NoProductError("No variants found")
    title = product.get("title", "")
    results = []
    for v in variants:
        available = bool(v.get("available", False))
        results.append({
            "name":         variant_name(title, v.get("title", "")),
            "sku":          v.get("sku") or "",
            "price":        format_cents(v.get("price")),
            "in_stock":     available,
            "stock_status": "in_stock" if available else "out_of_stock",
            "source":       "shopify_rest",
            "url":          url,
        })
    return results


def _parse_offer(offer: dict) -> tuple:
    availability = offer.get("availability", "")
    if "InStock" in availability:
        return "in_stock", True
    if "OutOfStock" in availability:
        return "out_of_stock", False
    if "PreOrder" in availability:
        return "preorder", False
    return "unknown", None


def parse_json_ld(url, html) -> list[dict]:
    """Parse all product offers from a page via Schema.org JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            items = [data] if isinstance(data, dict) else data
            for item in items:
                if item.get("@type") != "Product":
                    continue
                offers = item.get("offers", {})
                if not isinstance(offers, list):
                    offers = [offers] if offers else []
                results = []
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue
                    stock_status, in_stock = _parse_offer(offer)
                    results.append({
                        "name":         item.get("name", ""),
                        "sku":          offer.get("sku") or item.get("sku", ""),
                        "price":        str(offer.get("price", "")),
                        "in_stock":     in_stock,
                        "stock_status": stock_status,
                        "source":       "json_ld",
                        "url":          offer.get("url") or url,
                    })
                if results:
                    return results
        except json.JSONDecodeError:
            continue
    raise NoProductError("No JSON-LD Product found")

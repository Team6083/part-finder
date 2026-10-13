import json
import requests
from decimal import Decimal, InvalidOperation
from bs4 import BeautifulSoup
from urllib.parse import urlparse

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _fetch(url, **kwargs):
    r = requests.get(url, headers=HEADERS, timeout=10, **kwargs)
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
        raise ValueError("No variants found")
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
    raise ValueError("No JSON-LD Product found")

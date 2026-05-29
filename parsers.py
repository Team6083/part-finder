import json
import requests
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


def parse_shopify_rest(url) -> list[dict]:
    """Fetch all variants of a Shopify product via the public REST .json endpoint."""
    parsed = urlparse(url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    json_url = clean if clean.endswith(".json") else f"{clean}.json"
    r = _fetch(json_url)
    product = r.json().get("product", {})
    variants = product.get("variants", [])
    if not variants:
        raise ValueError("No variants found")
    title = product.get("title", "")
    results = []
    for v in variants:
        available = v.get("available", False)
        variant_title = v.get("title", "")
        name = title if not variant_title or variant_title == "Default Title" \
               else f"{title} – {variant_title}"
        results.append({
            "name":         name,
            "sku":          v.get("sku", ""),
            "price":        str(v.get("price", "")),
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

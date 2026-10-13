import logging
import re
from cache import ResultCache
from tools import (
    SearchTool, CachedTool, GenericUrlTool,
    ShopifyHandleTool, ShopifyStorefrontTool, ShopifyCatalogTool,
    JsonLdHandleTool, BigCommerceSearchTool,
)

log = logging.getLogger(__name__)


def sku(pattern: str):
    """Condition: matches bare SKU tokens."""
    pat = re.compile(pattern, re.IGNORECASE)
    return lambda token, is_url: not is_url and bool(pat.match(token))


def domain(pattern: str):
    """Condition: matches URLs whose domain contains pattern."""
    pat = re.compile(pattern, re.IGNORECASE)
    return lambda token, is_url: is_url and bool(pat.search(token))


class Rule:
    def __init__(self, tool: SearchTool, *conditions):
        self.tool = tool
        self._conditions = conditions

    def matches(self, token: str, is_url: bool) -> bool:
        return any(c(token, is_url) for c in self._conditions)


def build_rules(
    cache: ResultCache | None = None,
    ttl: float | None = None,
) -> list[Rule]:
    def w(tool, cacheable=True):
        return CachedTool(tool, cache, ttl) if (cache and cacheable) else tool

    def storefront(base):
        # Storefront search first; the old full-catalog crawl is the last resort.
        return ShopifyStorefrontTool(base, fallback=ShopifyCatalogTool(base))

    return [
        Rule(w(ShopifyHandleTool("https://wcproducts.com",
                                 fallback=ShopifyStorefrontTool("https://wcproducts.com"))),
             sku(r"^wcp-\d+$"),            domain(r"wcproducts\.com")),
        Rule(w(storefront("https://www.andymark.com")),
             sku(r"^am-\d+"),              domain(r"andymark\.com")),
        Rule(w(JsonLdHandleTool("https://www.revrobotics.com")),
             sku(r"^rev-\d+-\d+"),         domain(r"revrobotics\.com")),
        Rule(w(storefront("https://www.thethriftybot.com")),
             sku(r"^ttb-\d+"),             domain(r"thethriftybot\.com")),
        Rule(w(BigCommerceSearchTool("https://www.gobilda.com")),
             sku(r"^\d{4}-\d{4}-\d{4}$"), domain(r"gobilda\.com")),
    ]


_fallback = GenericUrlTool()


def resolve(token: str, rules: list[Rule]):
    """Return (search_fn, query). Caller just does search_fn(query)."""
    is_url = token.startswith("http://") or token.startswith("https://")
    for rule in rules:
        if rule.matches(token, is_url):
            tool = rule.tool
            log.debug("route %s -> %s", token, tool.name)
            return (tool.search_url, token) if is_url else (tool.search, token.upper())
    log.debug("route %s -> %s (no rule matched)", token, _fallback.name)
    return (_fallback.search_url, token) if is_url else (_fallback.search, token)

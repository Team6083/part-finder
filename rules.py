import logging
import re
from cache import ResultCache, UrlMap
from parsers import host_matches, url_host
from tools import (
    SearchTool, CachedTool, GenericUrlTool,
    ShopifyHandleTool, ShopifyStorefrontTool, ShopifyCatalogTool,
    JsonLdHandleTool, BigCommerceSearchTool,
)

log = logging.getLogger(__name__)


_SAFE_SKU = re.compile(r"[\w.\-]+")


def sku(pattern: str):
    """Condition: matches bare SKU tokens (letters, digits, _ . - only)."""
    pat = re.compile(pattern, re.IGNORECASE)
    return lambda token, is_url: not is_url and bool(_SAFE_SKU.fullmatch(token) and pat.match(token))


def domain(*hosts: str):
    """Condition: matches URLs whose hostname is one of hosts or a subdomain of one."""
    hosts = tuple(h.lower() for h in hosts)

    def cond(token, is_url):
        host = url_host(token) if is_url else None
        return host is not None and host_matches(host, hosts)

    cond.hosts = hosts
    return cond


class Rule:
    def __init__(self, tool: SearchTool, *conditions):
        self.tool = tool
        self._conditions = conditions

    def matches(self, token: str, is_url: bool) -> bool:
        return any(c(token, is_url) for c in self._conditions)

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(h for c in self._conditions for h in getattr(c, "hosts", ()))


def allowed_hosts(rules: list[Rule]) -> frozenset[str]:
    """Every store host named by a rule's domain() condition."""
    return frozenset(h for r in rules for h in r.hosts)


def build_rules(
    cache: ResultCache | None = None,
    ttl: float | None = None,
    map_ttl: float | None = None,
) -> list[Rule]:
    # Two cache layers over one backend: short-lived results (price/stock) and
    # long-lived SKU -> product URL mappings that skip the search step.
    url_map = UrlMap(cache, map_ttl) if cache else None

    def w(tool, cacheable=True):
        return CachedTool(tool, cache, ttl) if (cache and cacheable) else tool

    def storefront(base):
        # Storefront search first; the old full-catalog crawl is the last resort.
        return ShopifyStorefrontTool(base, fallback=ShopifyCatalogTool(base), url_map=url_map)

    return [
        Rule(w(ShopifyHandleTool("https://wcproducts.com",
                                 fallback=ShopifyStorefrontTool("https://wcproducts.com", url_map=url_map))),
             sku(r"^wcp-\d+$"),            domain("wcproducts.com")),
        Rule(w(storefront("https://www.andymark.com")),
             sku(r"^am-\d+"),              domain("andymark.com")),
        Rule(w(BigCommerceSearchTool("https://www.revrobotics.com", url_map=url_map,
                                       url_fn=lambda sku: f"https://www.revrobotics.com/{sku.lower()}/")),
             sku(r"^rev-\d+-\d+"),         domain("revrobotics.com")),
        Rule(w(storefront("https://www.thethriftybot.com")),
             sku(r"^ttb-\d+"),             domain("thethriftybot.com")),
        Rule(w(BigCommerceSearchTool("https://www.gobilda.com", url_map=url_map)),
             sku(r"^\d{4}-\d{4}-\d{4}$"), domain("gobilda.com")),
    ]


_fallback = GenericUrlTool()


class UnsupportedInput(ValueError):
    """The token matches no rule and the generic URL fallback is disabled."""


def resolve(token: str, rules: list[Rule], allow_generic: bool = True):
    """Return (search_fn, query). Caller just does search_fn(query).

    With allow_generic=False, input matching no rule raises UnsupportedInput instead of
    being handed to GenericUrlTool (which fetches any host)."""
    is_url = url_host(token) is not None
    for rule in rules:
        if rule.matches(token, is_url):
            tool = rule.tool
            log.debug("route %s -> %s", token, tool.name)
            return (tool.search_url, token) if is_url else (tool.search, token.upper())
    if not allow_generic:
        raise UnsupportedInput(token)
    log.debug("route %s -> %s (no rule matched)", token, _fallback.name)
    return (_fallback.search_url, token) if is_url else (_fallback.search, token)

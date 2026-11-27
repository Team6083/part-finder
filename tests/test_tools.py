import unittest
from unittest import mock

import requests

import argparse
import os
import stat
import tempfile

import main
from cache import MemoryCache, FileCache, UrlMap
from rules import build_rules, resolve
from parsers import FetchError, format_cents, parse_shopify_rest
from tools import (
    CachedTool, ShopifyCatalogTool, ShopifyStorefrontTool, BigCommerceSearchTool,
    UpcTool, normalize_upc, same_upc,
)


def _resp(status=200, payload=None, json_error=False):
    r = mock.Mock(status_code=status, headers={})
    r.iter_content.side_effect = lambda *_a, **_k: iter([b""])
    r.raise_for_status.side_effect = (
        requests.HTTPError(f"HTTP {status}") if status >= 400 else None
    )
    if json_error:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload
    return r


def _page(handle, sku):
    return {"products": [{
        "handle": handle, "title": handle,
        "variants": [{"sku": sku, "title": "Default Title", "price": "1.00", "available": True}],
    }]}


class FormatCentsTest(unittest.TestCase):
    def test_cents(self):
        self.assertEqual(format_cents(999), "9.99")
        self.assertEqual(format_cents(1000), "10.00")
        self.assertEqual(format_cents(5), "0.05")


class ShopifyRestTest(unittest.TestCase):
    @mock.patch("parsers._fetch")
    def test_uses_js_endpoint_and_available(self, fetch):
        fetch.return_value.json.return_value = {
            "title": "Bits",
            "variants": [{"sku": "WCP-1", "title": "Default Title", "price": 999, "available": False}],
        }
        out = parse_shopify_rest("https://x.com/products/wcp-1.json?foo=1")
        fetch.assert_called_once_with("https://x.com/products/wcp-1.js")
        self.assertEqual(out[0]["price"], "9.99")
        self.assertIs(out[0]["in_stock"], False)
        self.assertEqual(out[0]["stock_status"], "out_of_stock")


class CatalogRetryTest(unittest.TestCase):
    def setUp(self):
        self.tool = ShopifyCatalogTool("https://s.com")
        p = mock.patch("tools.time.sleep")
        p.start()
        self.addCleanup(p.stop)

    @mock.patch("tools.requests.get")
    def test_retries_then_succeeds(self, get):
        get.side_effect = [
            _resp(200, _page("a", "A-1")),
            _resp(429),
            _resp(200, json_error=True),
            _resp(200, {"products": []}),
        ]
        self.tool._build()
        self.assertIn("A-1", self.tool._catalog)
        self.assertEqual(get.call_count, 4)

    @mock.patch("tools.requests.get")
    def test_failure_does_not_store_partial_catalog(self, get):
        get.side_effect = [_resp(200, _page("a", "A-1"))] + [_resp(429)] * 3
        result, error = self.tool.search("A-1")
        self.assertIsNone(result)
        self.assertIn("failed after", error)
        self.assertIsNone(self.tool._catalog)

        self.tool._failed_at -= ShopifyCatalogTool.COOLDOWN  # cooldown elapsed
        get.side_effect = [_resp(200, _page("a", "A-1")), _resp(200, {"products": []})]
        result, error = self.tool.search("A-1")
        self.assertEqual(result["sku"], "A-1")


def _node(handle, skus, has_next=False):
    return {"handle": handle, "title": handle, "variants": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": "c"},
        "nodes": [{"sku": s, "title": "Default Title", "availableForSale": True,
                   "price": {"amount": "167.0"}} for s in skus]}}


def _http_error(status):
    return requests.HTTPError(f"HTTP {status}", response=mock.Mock(status_code=status))


class StorefrontTest(unittest.TestCase):
    _node = staticmethod(_node)

    @mock.patch("tools.requests.post")
    def test_requires_exact_sku_match(self, post):
        post.return_value = _resp(200, {"data": {"search": {"nodes": [
            self._node("wrong", ["AM-1"]), self._node("right", ["AM-2"])]}}})
        r, e = ShopifyStorefrontTool("https://s.com").search("am-2")
        self.assertEqual(r["url"], "https://s.com/products/right")
        self.assertEqual(r["price"], "167.00")
        self.assertEqual(r["source"], "shopify_storefront")

    @mock.patch("tools.requests.post")
    def test_no_exact_match_uses_fallback(self, post):
        post.return_value = _resp(200, {"data": {"search": {"nodes": [self._node("x", ["AM-1"])]}}})
        fb = mock.Mock()
        fb.search.return_value = ({"sku": "AM-2"}, None)
        r, e = ShopifyStorefrontTool("https://s.com", fallback=fb).search("AM-2")
        self.assertEqual(r, {"sku": "AM-2"})

    @mock.patch("tools.requests.post")
    def test_graphql_errors_use_fallback(self, post):
        post.return_value = _resp(200, {"errors": [{"message": "boom"}]})
        fb = mock.Mock()
        fb.search.return_value = (None, "nope")
        r, e = ShopifyStorefrontTool("https://s.com", fallback=fb).search("AM-2")
        fb.search.assert_called_once_with("AM-2")

    @mock.patch("tools.requests.post")
    def test_paginates_variants(self, post):
        post.side_effect = [
            _resp(200, {"data": {"search": {"nodes": [self._node("big", ["AM-1"], has_next=True)]}}}),
            _resp(200, {"data": {"product": {"variants": self._node("big", ["AM-2"])["variants"]}}}),
        ]
        r, e = ShopifyStorefrontTool("https://s.com").search("AM-2")
        self.assertEqual(r["sku"], "AM-2")


class UrlMapTest(unittest.TestCase):
    def test_roundtrip_delete_and_ttl(self):
        m = UrlMap(MemoryCache(), ttl=100)
        m.set("s", "am-1", "https://s.com/products/a")
        self.assertEqual(m.get("s", "AM-1"), "https://s.com/products/a")
        m.delete("s", "am-1")
        self.assertIsNone(m.get("s", "am-1"))
        m.set("s", "am-1", "https://s.com/products/a")
        with mock.patch("cache.time.time", return_value=10**10):
            self.assertIsNone(m.get("s", "am-1"))

    def test_file_cache_delete(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            c = FileCache(os.path.join(d, "c.json"))
            c.delete("missing")
            c.set("k", {"url": "u"})
            c.delete("k")
            self.assertIsNone(c.get("k", None))


class StorefrontUrlMapTest(unittest.TestCase):
    """Checks the SKU -> URL cache layer."""
    _node = staticmethod(_node)

    def setUp(self):
        self.map = UrlMap(MemoryCache(), ttl=None)
        self.tool = ShopifyStorefrontTool("https://s.com", url_map=self.map)

    def _js(self, sku):
        return mock.Mock(json=mock.Mock(return_value={
            "title": "T", "variants": [{"sku": sku, "title": "Default Title",
                                        "price": 100, "available": True}]}))

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_second_lookup_skips_search(self, post, fetch):
        post.return_value = _resp(200, {"data": {"search": {"nodes": [self._node("h", ["AM-2"])]}}})
        fetch.return_value = self._js("AM-2")
        self.tool.search("AM-2")
        self.assertEqual(self.map.get("https://s.com", "AM-2"), "https://s.com/products/h")
        r, e = self.tool.search("AM-2")
        self.assertEqual(post.call_count, 1)
        fetch.assert_called_once_with("https://s.com/products/h.js")
        self.assertEqual(r["sku"], "AM-2")
        self.assertEqual(r["price"], "1.00")

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_stale_mapping_is_dropped_and_researched(self, post, fetch):
        self.map.set("https://s.com", "AM-2", "https://s.com/products/gone")
        fetch.side_effect = _http_error(404)
        post.return_value = _resp(200, {"data": {"search": {"nodes": [self._node("new", ["AM-2"])]}}})
        r, e = self.tool.search("AM-2")
        self.assertEqual(r["url"], "https://s.com/products/new")
        self.assertEqual(self.map.get("https://s.com", "AM-2"), "https://s.com/products/new")

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_mapping_with_sku_mismatch_is_dropped(self, post, fetch):
        self.map.set("https://s.com", "AM-2", "https://s.com/products/old")
        fetch.return_value = self._js("OTHER")
        post.return_value = _resp(200, {"data": {"search": {"nodes": []}}})
        r, e = self.tool.search("AM-2")
        self.assertIsNone(r)
        self.assertIsNone(self.map.get("https://s.com", "AM-2"))


class BigCommerceUrlMapTest(unittest.TestCase):
    LD = ('<script type="application/ld+json">{"@type":"Product","name":"N",'
          '"offers":[{"sku":"1000-0001-0001","price":"1","url":"u"}]}</script>')
    SEARCH = '<li class="product"><a class="card" href="https://g.com/p/?x=1">p</a></li>'

    @mock.patch("tools._fetch")
    def test_mapped_lookup_skips_search_page(self, fetch):
        m = UrlMap(MemoryCache(), ttl=None)
        tool = BigCommerceSearchTool("https://g.com", url_map=m)
        fetch.side_effect = [mock.Mock(text=self.SEARCH), mock.Mock(text=self.LD)]
        tool.search("1000-0001-0001")
        self.assertEqual(m.get("https://g.com", "1000-0001-0001"), "https://g.com/p/")
        fetch.reset_mock()
        fetch.side_effect = [mock.Mock(text=self.LD)]
        r, e = tool.search("1000-0001-0001")
        fetch.assert_called_once_with("https://g.com/p/")
        self.assertEqual(r["sku"], "1000-0001-0001")


class BigCommerceVariantTest(unittest.TestCase):
    """REV-style pages: JSON-LD is a price range, variants live in product options."""

    PAGE = ('<h1>Couplers</h1><input name="product_id" value="820">'
            '<label data-product-attribute-value="1916" for="attribute_radio_422_1916">Kit (REV-21-2108)</label>'
            '<script type="application/ld+json">{"@type":"Product","name":"Couplers","sku":820,'
            '"offers":{"price":"8.75","url":"u"}}</script>')
    SEARCH = '<li class="product"><article class="card"><figure class="card-figure"><a href="https://r.com/c/?q=1">c</a></figure></article></li>'

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_variant_sku_resolved_via_options(self, fetch, post):
        fetch.side_effect = [mock.Mock(text=self.SEARCH), mock.Mock(text=self.PAGE)]
        post.return_value = _resp(200, {"data": {"sku": "REV-21-2108", "instock": True,
                                                 "price": {"without_tax": {"value": 8.75}}}})
        r, e = BigCommerceSearchTool("https://r.com").search("REV-21-2108")
        self.assertIsNone(e)
        self.assertEqual((r["sku"], r["price"], r["in_stock"], r["url"]),
                         ("REV-21-2108", "8.75", True, "https://r.com/c/"))
        self.assertEqual(post.call_args.kwargs["data"]["attribute[422]"], "1916")


class UpcTest(unittest.TestCase):
    SEARCH = ('<li class="product"><article class="card"><figure class="card-figure">'
              '<a href="https://r.com/{}/?searchuuid=1">c</a></figure></article></li>')

    @staticmethod
    def _single(sku, upc):
        return ('<script type="application/ld+json">{"@type":"Product","name":"Motor","sku":"%s",'
                '"offers":{"price":"90","availability":"https://schema.org/InStock"}}</script>'
                '<script type="text/javascript">\nvar BCData = {"product_attributes":'
                '{"sku":"%s","upc":"%s","gtin":null,"stock":null}};\n</script>' % (sku, sku, upc))

    VARIANTS = ('<h1>Couplers</h1><input name="product_id" value="820">'
                '<label data-product-attribute-value="1" for="attribute_radio_422_1">A (REV-21-2108)</label>'
                '<label data-product-attribute-value="2" for="attribute_radio_422_2">B (REV-21-2110)</label>'
                '<script type="application/ld+json">{"@type":"Product","name":"Couplers","sku":820,'
                '"offers":{"price":"8.75","url":"u"}}</script>'
                '<script>var BCData = {"product_attributes":{"sku":"REV-21-2108","upc":"722787607325"}};</script>')

    @staticmethod
    def _api(sku, upc):
        return _resp(200, {"data": {"sku": sku, "upc": upc, "instock": True,
                                    "price": {"without_tax": {"value": 8.75}}}})

    def test_normalize(self):
        self.assertEqual(normalize_upc("722787604379"), "722787604379")
        self.assertEqual(normalize_upc("0722787604379"), "722787604379")
        self.assertEqual(normalize_upc("00722787604379"), "722787604379")
        self.assertEqual(normalize_upc("4006381333931"), "4006381333931")
        self.assertTrue(same_upc("0722787604379", "722787604379"))
        self.assertFalse(same_upc(None, "722787604379"))

    def test_routing(self):
        rules = build_rules()
        for code in ("722787604379", "0722787604379", "00722787604379"):
            fn, q = resolve(code, rules, allow_generic=False)
            self.assertIsInstance(fn.__self__, UpcTool)
        fn, _ = resolve("1310-0016-4008", rules, allow_generic=False)
        self.assertNotIsInstance(fn.__self__, UpcTool)

    @mock.patch("tools._fetch")
    def test_single_product_page(self, fetch):
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("rev-21-1652")),
                             mock.Mock(text=self._single("REV-21-1652", "722787604379"))]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787604379")
        self.assertIsNone(e)
        self.assertEqual((r["sku"], r["url"]), ("REV-21-1652", "https://r.com/rev-21-1652/"))
        self.assertIn("search_query=722787604379", fetch.call_args_list[0].args[0])

    @mock.patch("tools._fetch")
    def test_page_with_other_upc_is_a_mismatch(self, fetch):
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("x")),
                             mock.Mock(text=self._single("REV-21-1652", "722787604379"))]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787600000")
        self.assertIsNone(r)
        self.assertIn("mismatch", e)

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_variant_matched_by_option_upc(self, fetch, post):
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("c")), mock.Mock(text=self.VARIANTS)]
        post.side_effect = [self._api("REV-21-2108", "722787607325"), self._api("REV-21-2110", "722787607332")]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787607332")
        self.assertIsNone(e)
        self.assertEqual((r["sku"], r["source"]), ("REV-21-2110", "bigcommerce_variant"))

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_variant_stops_at_first_match(self, fetch, post):
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("c")), mock.Mock(text=self.VARIANTS)]
        post.side_effect = [self._api("REV-21-2108", "722787607325")]
        r, _ = BigCommerceSearchTool("https://r.com").search_upc("722787607325")
        self.assertEqual(r["sku"], "REV-21-2108")
        self.assertEqual(post.call_count, 1)

    @mock.patch("tools._fetch")
    def test_url_map_keyed_by_upc(self, fetch):
        url_map = UrlMap(MemoryCache())
        tool = BigCommerceSearchTool("https://r.com", url_map=url_map)
        page = mock.Mock(text=self._single("REV-21-1652", "722787604379"))
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("rev-21-1652")), page, page]
        tool.search_upc("722787604379")
        self.assertEqual(url_map.get("https://r.com", "UPC:722787604379"), "https://r.com/rev-21-1652/")
        self.assertIsNone(url_map.get("https://r.com", "722787604379"))
        r, _ = tool.search_upc("722787604379")
        self.assertEqual(r["sku"], "REV-21-1652")
        self.assertEqual(fetch.call_count, 3)  # second lookup skipped the search

    def test_upc_tool_tries_stores_in_order(self):
        a, b = mock.Mock(), mock.Mock()
        a.name, b.name = "a", "b"
        a.search_upc.return_value = (None, "UPC 722787604379 not found on https://a")
        b.search_upc.return_value = ({"sku": "REV-21-1652"}, None)
        r, e = UpcTool([a, b]).search("0722787604379")
        self.assertEqual(r["sku"], "REV-21-1652")
        self.assertEqual(a.search_upc.call_args.args[0], "722787604379")

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_failing_option_does_not_hide_a_later_match(self, fetch, post):
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("c")), mock.Mock(text=self.VARIANTS)]
        post.side_effect = [requests.Timeout("slow"), self._api("REV-21-2110", "722787607332")]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787607332")
        self.assertIsNone(e)
        self.assertEqual(r["sku"], "REV-21-2110")

    @mock.patch("tools.requests.post", side_effect=requests.Timeout("slow"))
    @mock.patch("tools._fetch")
    def test_all_options_failing_is_an_error_not_a_miss(self, fetch, post):
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("c")), mock.Mock(text=self.VARIANTS)]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787607332")
        self.assertIsNone(r)
        self.assertEqual(post.call_count, 2)
        self.assertIn("slow", e)

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_option_page_never_falls_back_to_the_base_product(self, fetch, post):
        # Page UPC matches the default variant, but its SKU isn't in the JSON-LD (numeric id)
        # and no option answers with that UPC: not a match, not {"sku": 820}.
        page = self.VARIANTS.replace('"sku":"REV-21-2108","upc"', '"sku":"REV-X","upc"')
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("c")), mock.Mock(text=page)]
        post.side_effect = [self._api("REV-21-2108", "1"), self._api("REV-21-2110", "2")]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787607325")
        self.assertIsNone(r)
        self.assertIn("mismatch", e)

    @mock.patch("tools._fetch")
    def test_json_ld_of_another_variant_is_not_returned(self, fetch):
        # BCData says the UPC belongs to OTHER; the JSON-LD shows REV-21-1652: not a match.
        page = self._single("REV-21-1652", "722787604379").replace(
            '"product_attributes":{"sku":"REV-21-1652"', '"product_attributes":{"sku":"OTHER"')
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("p")), mock.Mock(text=page)]
        r, e = BigCommerceSearchTool("https://r.com").search_upc("722787604379")
        self.assertIsNone(r)
        self.assertIn("mismatch", e)

    @mock.patch("tools._fetch")
    def test_json_ld_is_used_when_bcdata_names_no_sku(self, fetch):
        page = self._single("REV-21-1652", "722787604379").replace(
            '"product_attributes":{"sku":"REV-21-1652"', '"product_attributes":{"sku":null')
        fetch.side_effect = [mock.Mock(text=self.SEARCH.format("p")), mock.Mock(text=page)]
        r, _ = BigCommerceSearchTool("https://r.com").search_upc("722787604379")
        self.assertEqual(r["sku"], "REV-21-1652")

    def test_page_attributes_missing_or_broken(self):
        pa = BigCommerceSearchTool._page_attributes
        self.assertEqual(pa("<html></html>"), {})
        self.assertEqual(pa("<script>var BCData = {broken</script>"), {})
        self.assertEqual(pa('<script>var BCData = {"product_attributes": []};</script>'), {})
        self.assertEqual(pa('<script>var BCData = {"product_attributes":{"upc":"1"}};</script>'), {"upc": "1"})

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_spent_budget_stops_option_probing(self, fetch, post):
        now = [0]
        pages = iter([self.SEARCH.format("c"), self.VARIANTS])

        def slow_fetch(url):
            text = next(pages)
            if text is self.VARIANTS:
                now[0] = 100  # the product page took us past the deadline
            return mock.Mock(text=text)

        fetch.side_effect = slow_fetch
        with mock.patch("tools.time.monotonic", side_effect=lambda: now[0]):
            r, e = BigCommerceSearchTool("https://r.com").search_upc("722787607332", deadline=10)
        self.assertIsNone(r)
        self.assertIn("budget", e)
        post.assert_not_called()

    def test_upc_tool_budget_skips_remaining_stores(self):
        a, b = mock.Mock(), mock.Mock()
        a.name, b.name = "a", "b"
        a.search_upc.return_value = (None, "UPC lookup time budget exceeded")
        b.search_upc.side_effect = lambda upc, deadline: BigCommerceSearchTool("https://b").search_upc(upc, deadline)
        clock = iter([0] + [100] * 10)  # the budget is spent by the time b starts
        with mock.patch("tools._fetch") as fetch, \
                mock.patch("tools.time.monotonic", side_effect=lambda: next(clock)):
            _, e = UpcTool([a, b], budget=1).search("722787604379")
            fetch.assert_not_called()
        self.assertIn("budget", e)
        self.assertNotIn("not found", e)

    @mock.patch("tools.requests.post")
    def test_option_post_refuses_redirects(self, post):
        post.return_value = _resp(302)
        with self.assertRaises(FetchError):
            BigCommerceSearchTool("https://r.com")._variant("u", "820", "n", "422", "1", "A (REV-21-2108)")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_budget_validation(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                UpcTool([], budget=bad)
        for env, want in (("", UpcTool.DEFAULT_BUDGET), ("12.5", 12.5)):
            with mock.patch.dict("os.environ", {"PART_FINDER_UPC_BUDGET": env}):
                self.assertEqual(UpcTool([])._budget, want)
        for env in ("abc", "0", "nan"):
            with mock.patch.dict("os.environ", {"PART_FINDER_UPC_BUDGET": env}), \
                    self.assertRaises(ValueError):
                UpcTool([])

    def test_upc_tool_reports_failure_over_miss(self):
        a, b = mock.Mock(), mock.Mock()
        a.name, b.name = "a", "b"
        a.search_upc.return_value = (None, "UPC mismatch: 1 not found in search results")
        b.search_upc.return_value = (None, "Read timed out")
        _, e = UpcTool([a, b]).search("722787604379")
        self.assertIn("timed out", e)
        b.search_upc.return_value = (None, "UPC 722787604379 not found on https://b")
        _, e = UpcTool([a, b]).search("722787604379")
        self.assertEqual(e, "UPC 722787604379 not found")


class MissCacheTest(unittest.TestCase):
    """CachedTool(miss_ttl=...) remembers misses, never upstream failures."""

    def _tool(self, *answers, miss_ttl=600):
        inner = mock.Mock()
        inner.name = "inner"
        inner.search.side_effect = list(answers)
        return inner, CachedTool(inner, MemoryCache(), ttl=600, miss_ttl=miss_ttl)

    def test_miss_is_cached(self):
        inner, tool = self._tool((None, "UPC 722787600000 not found"))
        self.assertEqual(tool.search("722787600000"), (None, "UPC 722787600000 not found"))
        self.assertEqual(tool.search("722787600000"), (None, "UPC 722787600000 not found"))
        self.assertEqual(inner.search.call_count, 1)

    def test_upstream_error_is_not_cached(self):
        inner, tool = self._tool((None, "Read timed out"), ({"sku": "X"}, None))
        tool.search("722787600000")
        self.assertEqual(tool.search("722787600000"), ({"sku": "X"}, None))
        self.assertEqual(inner.search.call_count, 2)

    def test_misses_not_cached_without_miss_ttl(self):
        inner, tool = self._tool((None, "SKU X not found"), (None, "SKU X not found"), miss_ttl=None)
        tool.search("X")
        tool.search("X")
        self.assertEqual(inner.search.call_count, 2)

    def test_miss_expires(self):
        inner, tool = self._tool((None, "UPC 1 not found"), ({"sku": "X"}, None))
        with mock.patch("cache.time.time", return_value=1000):
            tool.search("1")
        with mock.patch("cache.time.time", return_value=1000 + 601):
            self.assertEqual(tool.search("1"), ({"sku": "X"}, None))

    def test_upc_rule_caches_misses_sku_rules_do_not(self):
        rules = build_rules(MemoryCache())
        upc_tool = resolve("722787604379", rules, allow_generic=False)[0].__self__
        sku_tool = resolve("REV-21-1652", rules, allow_generic=False)[0].__self__
        self.assertTrue(upc_tool._miss_ttl)
        self.assertIsNone(sku_tool._miss_ttl)


class BigCommerceVariantEdgeTest(unittest.TestCase):
    URL = "https://r.com/c/"

    @staticmethod
    def _page(*labels):
        radios = "".join(f'<label data-product-attribute-value="{i}" for="attribute_radio_9_{i}">{l}</label>'
                         for i, l in enumerate(labels, 1))
        return ('<h1>Shafts</h1><input name="product_id" value="675">' + radios +
                '<script type="application/ld+json">{"@type":"Product","name":"Shafts","sku":675,'
                '"offers":{"price":"5","url":"u"}}</script>')

    @staticmethod
    def _api(sku, price=5):
        return _resp(200, {"data": {"sku": sku, "instock": True, "price": {"without_tax": {"value": price}}}})

    def _tool(self):
        return BigCommerceSearchTool("https://r.com")

    @mock.patch("tools.requests.post")
    def test_sku_first_label(self, post):
        post.return_value = self._api("REV-21-2809")
        page = self._page("(REV-21-2809) 1/2in Rounded Hex Shaft - 1.0in")
        self.assertEqual(self._tool()._variants(self.URL, page, "REV-21-2809")[0]["sku"], "REV-21-2809")

    @mock.patch("tools.requests.post")
    def test_multipack_label_matches_base_sku_only_without_exact(self, post):
        post.side_effect = [self._api("REV-21-2003-PK10"), self._api("REV-21-2003-PK50")]
        page = self._page("(REV-21-2003-PK10) Spacer - 10 Pack", "(REV-21-2003-PK50) Spacer - 50 Pack")
        v = self._tool()._variants(self.URL, page, "REV-21-2003")
        self.assertEqual(len(v), 2)
        self.assertEqual(post.call_count, 2)

    @mock.patch("tools.requests.post")
    def test_empty_api_answers_are_not_variants(self, post):
        page = self._page("Kit (REV-21-2108)")
        for body in ({"data": {"sku": None, "instock": True, "price": {"without_tax": {"value": 8}}}},
                     {"data": []},
                     {"data": {"sku": "OTHER-1", "instock": True}}):
            post.return_value = _resp(200, body)
            self.assertEqual(self._tool()._variants(self.URL, page, "REV-21-2108"), [])

    @mock.patch("tools.requests.post")
    def test_labels_without_sku_are_not_queried(self, post):
        self._tool()._variants(self.URL, self._page("Plain option"))
        post.assert_not_called()

    @mock.patch("tools.requests.post", side_effect=requests.Timeout("slow"))
    @mock.patch("tools._fetch")
    def test_search_url_falls_back_to_json_ld_when_post_fails(self, fetch, post):
        fetch.return_value = mock.Mock(text=self._page("Kit (REV-21-2108)"))
        r, e = self._tool().search_url(self.URL)
        self.assertIsNone(e)
        self.assertEqual(r[0]["sku"], 675)

    @mock.patch("tools.requests.post", side_effect=requests.Timeout("slow"))
    def test_post_failure_propagates_for_sku_lookup(self, post):
        with self.assertRaises(requests.Timeout):
            self._tool()._variants(self.URL, self._page("Kit (REV-21-2108)"), "REV-21-2108")


class BigCommerceDirectPageTest(unittest.TestCase):
    ADDON = ('<h1>SPARK MAX</h1><input name="product_id" value="360">'
             '<label data-product-attribute-value="1741" for="attribute_radio_434_1741">'
             '(REV-11-1228-PK10) Data Port Cover - 10 Pack</label>'
             '<script type="application/ld+json">{"@type":"Product","name":"SPARK MAX","sku":"REV-11-2158",'
             '"offers":{"price":"100","url":"u"}}</script>')

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_search_url_ignores_addon_options_when_json_ld_has_real_sku(self, fetch, post):
        fetch.return_value = mock.Mock(text=self.ADDON)
        r, e = BigCommerceSearchTool("https://r.com").search_url("https://r.com/rev-11-2158/")
        self.assertIsNone(e)
        self.assertEqual([v["sku"] for v in r], ["REV-11-2158"])
        post.assert_not_called()
        fetch.assert_called_once()

    @mock.patch("tools._fetch")
    def test_guessed_page_skips_search(self, fetch):
        fetch.return_value = mock.Mock(text=self.ADDON)
        tool = BigCommerceSearchTool("https://r.com", url_fn=lambda s: f"https://r.com/{s.lower()}/")
        r, e = tool.search("REV-11-2158")
        self.assertEqual(r["sku"], "REV-11-2158")
        fetch.assert_called_once_with("https://r.com/rev-11-2158/")

    @mock.patch("tools._fetch")
    def test_guess_404_falls_back_to_search(self, fetch):
        search = '<li class="product"><article class="card"><figure class="card-figure"><a href="https://r.com/p/">p</a></figure></article></li>'
        fetch.side_effect = [_http_error(404), mock.Mock(text=search),
                             mock.Mock(text=self.ADDON.replace("REV-11-2158", "REV-11-9999"))]
        tool = BigCommerceSearchTool("https://r.com", url_fn=lambda s: f"https://r.com/{s.lower()}/")
        r, e = tool.search("REV-11-9999")
        self.assertEqual(r["url"], "u")
        self.assertEqual(fetch.call_count, 3)


class BigCommerceReviewFollowUpTest(unittest.TestCase):
    URL = "https://r.com/p/"

    @staticmethod
    def _page(ld_sku, *labels):
        radios = "".join(f'<label data-product-attribute-value="{i}" for="attribute_radio_9_{i}">{l}</label>'
                         for i, l in enumerate(labels, 1))
        sku = f'"sku":"{ld_sku}",' if ld_sku is not None else ""
        return ('<h1>P</h1><input name="product_id" value="7">' + radios +
                '<script type="application/ld+json">{"@type":"Product","name":"P",' + sku +
                '"offers":{"price":"5","url":"u"}}</script>')

    @staticmethod
    def _api(sku):
        return _resp(200, {"data": {"sku": sku, "instock": True, "price": {"without_tax": {"value": 5}}}})

    def _tool(self):
        return BigCommerceSearchTool("https://r.com")

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_json_ld_sku_that_is_an_option_means_variants(self, fetch, post):
        # JSON-LD only shows the default variant (REV /1in-extrusion/): list every option.
        fetch.return_value = mock.Mock(text=self._page("REV-21-1000", "(REV-21-1000) A", "(REV-21-1404) B"))
        post.side_effect = [self._api("REV-21-1000"), self._api("REV-21-1404")]
        r, e = self._tool().search_url(self.URL)
        self.assertEqual([v["sku"] for v in r], ["REV-21-1000", "REV-21-1404"])

    @mock.patch("tools.requests.post")
    @mock.patch("tools._fetch")
    def test_missing_json_ld_sku_still_queries_options(self, fetch, post):
        fetch.return_value = mock.Mock(text=self._page(None, "Kit (REV-21-2108)"))
        post.return_value = self._api("REV-21-2108")
        r, e = self._tool().search_url(self.URL)
        self.assertEqual([v["sku"] for v in r], ["REV-21-2108"])

    @mock.patch("tools._fetch")
    def test_multipack_json_ld_sku_matches_base_sku(self, fetch):
        fetch.return_value = mock.Mock(text=self._page("REV-11-1228-PK10"))
        v = self._tool()._product(self.URL, "REV-11-1228")
        self.assertEqual(v["sku"], "REV-11-1228-PK10")

    @mock.patch("tools._fetch")
    def test_other_sku_still_mismatches(self, fetch):
        fetch.return_value = mock.Mock(text=self._page("REV-11-1228-PK10"))
        self.assertIsNone(self._tool()._product(self.URL, "REV-11-12"))

    def test_label_sku_ignores_parenthesised_words(self):
        f = BigCommerceSearchTool._label_sku
        self.assertEqual(f("Spacer (Non-Locking) (REV-21-1234)"), "REV-21-1234")
        self.assertEqual(f("(REV-21-2809) Shaft"), "REV-21-2809")
        self.assertIsNone(f("Spacer (Mid-Pack)"))


class MappedLookupFailureTest(unittest.TestCase):
    """Only a gone product drops a mapping; transient errors and cache faults must not."""

    URL = "https://s.com/products/h"

    def setUp(self):
        self.map = UrlMap(MemoryCache(), ttl=None)
        self.map.set("https://s.com", "AM-2", self.URL)
        self.tool = ShopifyStorefrontTool("https://s.com", url_map=self.map)

    def _search_hit(self, post):
        post.return_value = _resp(200, {"data": {"search": {"nodes": [_node("h", ["AM-2"])]}}})

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_transient_errors_keep_mapping(self, post, fetch):
        # Search finds nothing, so a surviving entry proves it was never deleted
        # (a successful search would re-store it and hide the bug).
        post.return_value = _resp(200, {"data": {"search": {"nodes": []}}})
        for err in (requests.Timeout("slow"), _http_error(429), _http_error(503)):
            fetch.side_effect = err
            self.tool.search("AM-2")
            self.assertEqual(self.map.get("https://s.com", "AM-2"), self.URL)

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_410_drops_mapping(self, post, fetch):
        post.return_value = _resp(200, {"data": {"search": {"nodes": []}}})
        fetch.side_effect = _http_error(410)
        self.tool.search("AM-2")
        self.assertIsNone(self.map.get("https://s.com", "AM-2"))

    @mock.patch("tools._fetch")
    def test_goBILDA_timeout_keeps_mapping(self, fetch):
        m = UrlMap(MemoryCache(), ttl=None)
        m.set("https://g.com", "1000-0001-0001", "https://g.com/p/")
        fetch.side_effect = requests.Timeout("slow")
        BigCommerceSearchTool("https://g.com", url_map=m).search("1000-0001-0001")
        self.assertEqual(m.get("https://g.com", "1000-0001-0001"), "https://g.com/p/")

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_broken_cache_does_not_break_lookup(self, post, fetch):
        class Broken(MemoryCache):
            def get(self, *a, **k):
                raise OSError("disk")
            set = delete = get
        tool = ShopifyStorefrontTool("https://s.com", url_map=UrlMap(Broken(), ttl=None))
        self._search_hit(post)
        r, e = tool.search("AM-2")
        self.assertIsNone(e)
        self.assertEqual(r["sku"], "AM-2")

    def test_cached_tool_survives_broken_cache(self):
        class Broken(MemoryCache):
            def get(self, *a, **k):
                raise OSError("disk")
            set = get
        inner = mock.Mock()
        inner.name = "inner"
        inner.search.return_value = ({"sku": "A"}, None)
        r, e = CachedTool(inner, Broken(), ttl=60).search("A")
        self.assertEqual(r, {"sku": "A"})


class FileCacheRobustnessTest(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "c.json")

    def test_corrupt_file_is_an_empty_cache(self):
        with open(self.path, "w") as f:
            f.write('{"a": {"result"')
        c = FileCache(self.path)
        self.assertIsNone(c.get("a", None))
        c.set("b", {"x": 1})
        self.assertEqual(c.get("b", None), {"x": 1})

    def test_concurrent_writes_lose_nothing(self):
        import threading
        c = FileCache(self.path)
        threads = [threading.Thread(target=c.set, args=(f"k{i}", {"i": i})) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([c.get(f"k{i}", None) for i in range(40)],
                         [{"i": i} for i in range(40)])

    def test_no_temp_files_left_behind(self):
        import os
        c = FileCache(self.path)
        c.set("a", {"x": 1})
        c.delete("a")
        self.assertEqual(os.listdir(self.dir.name), ["c.json"])


class FileCachePermissionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "c.json")

    def test_unreadable_file_is_not_wiped(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        c = FileCache(self.path)
        c.set("a", {"x": 1})
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o644)
        with self.assertRaises(OSError):
            c.set("b", {"x": 2})
        os.chmod(self.path, 0o644)
        self.assertEqual(c.get("a", None), {"x": 1})

    def test_mode_is_preserved(self):
        c = FileCache(self.path)
        c.set("a", {"x": 1})
        os.chmod(self.path, 0o640)
        c.set("b", {"x": 2})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_new_file_is_world_readable(self):
        FileCache(self.path).set("a", {"x": 1})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)


class StaleOnEmptyPageTest(unittest.TestCase):
    @mock.patch("tools._fetch")
    def test_goBILDA_200_without_json_ld_drops_mapping(self, fetch):
        m = UrlMap(MemoryCache(), ttl=None)
        m.set("https://g.com", "1000-0001-0001", "https://g.com/gone/")
        fetch.side_effect = [mock.Mock(text="<html>moved</html>"), mock.Mock(text="")]
        BigCommerceSearchTool("https://g.com", url_map=m).search("1000-0001-0001")
        self.assertIsNone(m.get("https://g.com", "1000-0001-0001"))

    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_shopify_non_json_page_keeps_mapping(self, post, fetch):
        m = UrlMap(MemoryCache(), ttl=None)
        m.set("https://s.com", "AM-2", "https://s.com/products/h")
        fetch.return_value.json.side_effect = requests.exceptions.JSONDecodeError("x", "", 0)
        post.return_value = _resp(200, {"data": {"search": {"nodes": []}}})
        ShopifyStorefrontTool("https://s.com", url_map=m).search("AM-2")
        self.assertIsNotNone(m.get("https://s.com", "AM-2"))


class MappedResultConsistencyTest(unittest.TestCase):
    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_mapped_result_equals_searched_result(self, post, fetch):
        post.return_value = _resp(200, {"data": {"search": {"nodes": [{
            "handle": "h", "title": "T", "variants": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"sku": "AM-2", "title": "Default Title",
                           "availableForSale": True, "price": {"amount": "167.0"}}]}}]}}})
        fetch.return_value.json.return_value = {"title": "T", "variants": [
            {"sku": "AM-2", "title": "Default Title", "price": 16700, "available": True}]}
        tool = ShopifyStorefrontTool("https://s.com", url_map=UrlMap(MemoryCache(), ttl=None))
        searched, _ = tool.search("AM-2")
        mapped, _ = tool.search("AM-2")
        self.assertEqual(fetch.call_count, 1)  # second call really used the map
        self.assertEqual(mapped, searched)


class WcpFallbackMapTest(unittest.TestCase):
    @mock.patch("parsers._fetch")
    @mock.patch("tools.requests.post")
    def test_wcp_fallback_is_mapped(self, post, fetch):
        cache = MemoryCache()
        rules = build_rules(cache, ttl=None, map_ttl=None)
        wcp = next(r.tool for r in rules if r.matches("WCP-9999", False))._tool
        fetch.side_effect = [requests.HTTPError("HTTP 404", response=mock.Mock(status_code=404))]
        post.return_value = _resp(200, {"data": {"search": {"nodes": [_node("odd-handle", ["WCP-9999"])]}}})
        r, e = wcp.search("WCP-9999")
        self.assertEqual(r["url"], "https://wcproducts.com/products/odd-handle")
        self.assertEqual(UrlMap(cache).get("https://wcproducts.com", "WCP-9999"),
                         "https://wcproducts.com/products/odd-handle")


class CacheFlagsTest(unittest.TestCase):
    def _args(self, **kw):
        ns = dict(cache_file=None, no_cache=False,
                  cache_ttl=main.DEFAULT_CACHE_TTL_HOURS, map_ttl=main.DEFAULT_MAP_TTL_HOURS)
        ns.update(kw)
        return argparse.Namespace(**ns)

    def test_defaults(self):
        cache, ttl, map_ttl = main._make_cache(self._args())
        self.assertIsNone(cache)
        self.assertEqual(ttl, 600)
        self.assertEqual(map_ttl, 7 * 86400)

    def test_serve_defaults_to_memory_cache(self):
        cache, _, _ = main._make_cache(self._args(), default_memory=True)
        self.assertIsInstance(cache, MemoryCache)

    def test_no_cache_wins_over_serve_default(self):
        cache, _, _ = main._make_cache(self._args(no_cache=True), default_memory=True)
        self.assertIsNone(cache)

    def test_zero_ttl_means_never_expire(self):
        _, ttl, map_ttl = main._make_cache(self._args(cache_ttl=0, map_ttl=0))
        self.assertIsNone(ttl)
        self.assertIsNone(map_ttl)

    def test_no_cache_and_cache_file_conflict(self):
        parser = argparse.ArgumentParser()
        main._add_cache_args(parser)
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            parser.parse_args(["--no-cache", "--cache-file", "x.json"])


class TwoLayerCacheTest(unittest.TestCase):
    def test_result_expires_while_mapping_survives(self):
        cache = MemoryCache()
        m = UrlMap(cache, ttl=7 * 86400)
        m.set("s", "A", "https://s.com/products/a")
        inner = mock.Mock()
        inner.name = "inner"
        inner.search.return_value = ({"sku": "A"}, None)
        tool = CachedTool(inner, cache, ttl=600)
        tool.search("A")
        tool.search("A")
        self.assertEqual(inner.search.call_count, 1)
        with mock.patch("cache.time.time", return_value=__import__("time").time() + 3600):
            tool.search("A")
            self.assertEqual(inner.search.call_count, 2)
            self.assertEqual(m.get("s", "A"), "https://s.com/products/a")


if __name__ == "__main__":
    unittest.main()

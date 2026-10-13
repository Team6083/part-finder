import unittest
from unittest import mock

import requests

from parsers import format_cents, parse_shopify_rest
from tools import ShopifyCatalogTool, ShopifyStorefrontTool


def _resp(status=200, payload=None, json_error=False):
    r = mock.Mock(status_code=status)
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

        get.side_effect = [_resp(200, _page("a", "A-1")), _resp(200, {"products": []})]
        result, error = self.tool.search("A-1")
        self.assertEqual(result["sku"], "A-1")


class StorefrontTest(unittest.TestCase):
    def _node(self, handle, skus, has_next=False):
        return {"handle": handle, "title": handle, "variants": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": "c"},
            "nodes": [{"sku": s, "title": "Default Title", "availableForSale": True,
                       "price": {"amount": "167.0"}} for s in skus]}}

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


if __name__ == "__main__":
    unittest.main()

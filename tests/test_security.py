import threading
import unittest
from unittest import mock

import requests
from fastapi.testclient import TestClient

import parsers
from cache import MemoryCache
from parsers import FetchError, _fetch, set_allowed_hosts
from rules import UnsupportedInput, allowed_hosts, build_rules, resolve
from server import create_app
from tools import CachedTool, GenericUrlTool, ShopifyCatalogTool, normalize_url


def _redirect(location, status=302):
    r = mock.Mock(status_code=status, headers={"Location": location})
    return r


def _ok(body=b"{}", headers=None):
    r = mock.Mock(status_code=200, headers=headers or {})
    r.raise_for_status.return_value = None
    r.iter_content.side_effect = lambda *_a, **_k: iter([body])
    return r


class HostPolicyMixin:
    def tearDown(self):
        set_allowed_hosts(None)


SSRF_URLS = [
    "http://127.0.0.1:6379/?x=andymark.com",
    "http://10.0.0.5/wcproducts.com",
    "HTTP://127.0.0.1:8000/docs",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1\\@andymark.com/",
    "http://andymark.com@127.0.0.1/",
    "https://evilandymark.com/",
    "http://127.0.0.1:8000/docs\n",
    "file:///etc/passwd",
]


class ServerSsrfTest(HostPolicyMixin, unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(cache=MemoryCache()))

    def test_ssrf_urls_rejected_without_network(self):
        with mock.patch("requests.get") as get, mock.patch("requests.post") as post:
            for url in SSRF_URLS:
                body = self.client.get("/lookup", params={"q": url}).json()
                self.assertEqual(body[0]["status"], "error", url)
                self.assertEqual(body[0]["error"], "unsupported input", url)
            batch = self.client.post("/lookup", json={"queries": SSRF_URLS + ["nonsense"]}).json()
            self.assertTrue(all(b["status"] == "error" for b in batch))
        get.assert_not_called()
        post.assert_not_called()


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.rules = build_rules()

    def tool_for(self, token):
        search_fn, _ = resolve(token, self.rules, allow_generic=False)
        return search_fn.__self__.name

    def test_store_urls_route(self):
        self.assertIn("wcproducts.com", self.tool_for("https://wcproducts.com/products/wcp-1838"))
        self.assertIn("andymark.com", self.tool_for("https://www.andymark.com/products/x"))
        self.assertIn("revrobotics.com", self.tool_for("HTTPS://WWW.REVROBOTICS.COM/rev-11-2159/"))
        self.assertIn("gobilda.com", self.tool_for("https://www.gobilda.com/x/?a=1"))
        self.assertIn("thethriftybot.com", self.tool_for("https://www.thethriftybot.com/products/y"))

    def test_skus_route(self):
        self.assertIn("wcproducts.com", self.tool_for("WCP-1838"))
        self.assertIn("andymark.com", self.tool_for("am-0255_15T"))
        self.assertIn("gobilda.com", self.tool_for("1310-0001-0001"))

    def test_unknown_rejected_when_generic_disabled(self):
        for token in ("https://example.com/", "hello world", "WCP-1\n", "am-1/../x"):
            with self.assertRaises(UnsupportedInput):
                resolve(token, self.rules, allow_generic=False)

    def test_generic_fallback_kept_for_cli(self):
        fn, _ = resolve("https://example.com/p", self.rules)
        self.assertIsInstance(fn.__self__, GenericUrlTool)

    def test_allowed_hosts_from_rules(self):
        self.assertEqual(allowed_hosts(self.rules), {
            "wcproducts.com", "andymark.com", "revrobotics.com", "thethriftybot.com", "gobilda.com"})


class FetchTest(HostPolicyMixin, unittest.TestCase):
    @mock.patch("parsers.requests.get")
    def test_redirect_to_disallowed_host_refused(self, get):
        set_allowed_hosts({"andymark.com"})
        get.return_value = _redirect("http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(FetchError):
            _fetch("https://www.andymark.com/x")
        self.assertEqual(get.call_count, 1)

    @mock.patch("parsers.requests.get")
    def test_redirect_within_allowlist_followed(self, get):
        set_allowed_hosts({"andymark.com"})
        get.side_effect = [_redirect("/y"), _ok(b"hi")]
        r = _fetch("https://www.andymark.com/x")
        self.assertEqual(r._content, b"hi")
        self.assertEqual(get.call_args.args[0], "https://www.andymark.com/y")
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)

    @mock.patch("parsers.requests.get")
    def test_no_allowlist_follows_any_host(self, get):
        get.side_effect = [_redirect("http://other.example/z"), _ok()]
        _fetch("http://a.example/x")
        self.assertEqual(get.call_args.args[0], "http://other.example/z")

    @mock.patch("parsers.requests.get")
    def test_redirect_loop_capped(self, get):
        get.return_value = _redirect("http://a.example/x")
        with self.assertRaises(FetchError):
            _fetch("http://a.example/x")
        self.assertEqual(get.call_count, parsers.MAX_REDIRECTS + 1)

    @mock.patch("parsers.requests.get")
    def test_body_size_capped(self, get):
        get.return_value = _ok(b"x" * 100)
        with self.assertRaises(FetchError):
            _fetch("http://a.example/x", max_bytes=10)
        get.return_value = _ok(b"x", headers={"Content-Length": "999"})
        with self.assertRaises(FetchError):
            _fetch("http://a.example/x", max_bytes=10)

    @mock.patch("parsers.requests.get")
    def test_non_http_scheme_refused(self, get):
        with self.assertRaises(FetchError):
            _fetch("file:///etc/passwd")
        get.assert_not_called()


class ServerLimitsTest(HostPolicyMixin, unittest.TestCase):
    def test_batch_limit(self):
        client = TestClient(create_app(max_batch=3))
        self.assertEqual(client.post("/lookup", json={"queries": ["nope"] * 3}).status_code, 200)
        self.assertEqual(client.post("/lookup", json={"queries": ["nope"] * 4}).status_code, 422)

    def test_batch_limit_from_env(self):
        with mock.patch.dict("os.environ", {"PART_FINDER_MAX_BATCH": "2"}):
            client = TestClient(create_app())
        self.assertEqual(client.post("/lookup", json={"queries": ["a"] * 3}).status_code, 422)

    def test_query_length_capped(self):
        client = TestClient(create_app())
        self.assertEqual(client.get("/lookup", params={"q": "a" * 2049}).status_code, 422)
        self.assertEqual(client.post("/lookup", json={"queries": ["a" * 2049]}).status_code, 422)

    def test_api_key_enforced_when_set(self):
        client = TestClient(create_app(api_key="s3cret"))
        self.assertEqual(client.get("/lookup", params={"q": "x"}).status_code, 401)
        self.assertEqual(client.get("/lookup", params={"q": "x"}, headers={"X-API-Key": "bad"}).status_code, 401)
        self.assertEqual(client.post("/lookup", json={"queries": ["x"]}).status_code, 401)
        self.assertEqual(client.get("/lookup", params={"q": "x"}, headers={"X-API-Key": "s3cret"}).status_code, 200)
        self.assertEqual(client.post("/lookup", json={"queries": ["x"]},
                                     headers={"X-API-Key": "s3cret"}).status_code, 200)

    def test_api_key_from_env(self):
        with mock.patch.dict("os.environ", {"PART_FINDER_API_KEY": "k"}):
            client = TestClient(create_app())
        self.assertEqual(client.get("/lookup", params={"q": "x"}).status_code, 401)

    def test_no_auth_when_unset(self):
        with mock.patch.dict("os.environ", clear=False) as env:
            env.pop("PART_FINDER_API_KEY", None)
            client = TestClient(create_app())
        self.assertEqual(client.get("/lookup", params={"q": "x"}).status_code, 200)


class ServerErrorTextTest(HostPolicyMixin, unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())

    @mock.patch("parsers.requests.get")
    def test_upstream_exception_text_hidden(self, get):
        get.side_effect = requests.ConnectionError("HTTPConnectionPool(host='db.internal', port=6379)")
        body = self.client.get("/lookup", params={"q": "REV-11-2159"}).json()
        self.assertEqual(body[0]["error"], "upstream error")
        self.assertNotIn("internal", str(body))
        self.assertNotIn("6379", str(body))

    @mock.patch("parsers.requests.get")
    def test_blocked_redirect_text_hidden(self, get):
        get.return_value = _redirect("http://10.1.2.3:9999/")
        body = self.client.get("/lookup", params={"q": "REV-11-2159"}).json()
        self.assertEqual(body[0]["error"], "upstream error")
        self.assertNotIn("10.1.2.3", str(body))

    @mock.patch("parsers.requests.get")
    def test_http_404_is_not_found(self, get):
        r = _ok()
        r.status_code = 404
        r.raise_for_status.side_effect = requests.HTTPError("404 Client Error: Not Found for url: http://x")
        get.return_value = r
        body = self.client.get("/lookup", params={"q": "REV-11-2159"}).json()
        self.assertEqual(body[0]["error"], "not found")

    def test_unexpected_exception_hidden(self):
        with mock.patch("server.resolve", side_effect=RuntimeError("secret detail")):
            body = self.client.get("/lookup", params={"q": "WCP-1"}).json()
        self.assertEqual(body[0]["error"], "upstream error")


class MemoryCacheLruTest(unittest.TestCase):
    def test_evicts_least_recently_used(self):
        c = MemoryCache(max_entries=2)
        c.set("a", {"v": 1})
        c.set("b", {"v": 2})
        c.get("a")
        c.set("c", {"v": 3})
        self.assertEqual(c.get("a"), {"v": 1})
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("c"), {"v": 3})

    def test_bounded_under_threads(self):
        c = MemoryCache(max_entries=50)

        def work(n):
            for i in range(500):
                c.set(f"{n}-{i}", {"v": i})
                c.get(f"{n}-{i // 2}")

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(c._store), 50)


class UrlCacheKeyTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_url("HTTPS://WWW.Example.com/Products/X?a=1#f"),
                         "https://www.example.com/Products/X")
        self.assertEqual(normalize_url("https://u:p@a.com/x?y"), "https://a.com/x")

    def test_query_variants_share_one_entry(self):
        cache = MemoryCache()
        inner = mock.Mock()
        inner.name = "inner"
        inner.search_url.return_value = ([{"sku": "A"}], None)
        tool = CachedTool(inner, cache)
        for i in range(5):
            tool.search_url(f"https://a.com/p?a={i}")
        self.assertEqual(inner.search_url.call_count, 1)
        self.assertEqual(len(cache._store), 1)


class CatalogBuildTest(unittest.TestCase):
    def setUp(self):
        self.tool = ShopifyCatalogTool("https://s.com")

    def test_concurrent_misses_build_once(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def fetch_page(page):
            calls.append(page)
            started.set()
            release.wait(5)
            return [{"handle": "a", "title": "a", "variants": [{"sku": "A-1", "price": "1"}]}] if page == 1 else []

        results = []
        with mock.patch.object(self.tool, "_fetch_page", side_effect=fetch_page), \
                mock.patch("tools.time.sleep"):
            threads = [threading.Thread(target=lambda: results.append(self.tool.search("A-1"))) for _ in range(8)]
            for t in threads:
                t.start()
            started.wait(5)
            release.set()
            for t in threads:
                t.join()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r[0]["sku"] == "A-1" for r in results))

    def test_failed_build_respects_cooldown(self):
        with mock.patch.object(self.tool, "_fetch_page", side_effect=RuntimeError("boom")) as fp:
            _, e1 = self.tool.search("A-1")
            _, e2 = self.tool.search("A-2")
            self.assertEqual(fp.call_count, 1)
            self.assertIn("boom", e1)
            self.assertIn("not retrying", e2)
            self.tool._failed_at -= ShopifyCatalogTool.COOLDOWN
            self.tool.search("A-3")
            self.assertEqual(fp.call_count, 2)


if __name__ == "__main__":
    unittest.main()

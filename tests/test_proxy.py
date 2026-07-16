import io
import json
import unittest
import urllib.error
from unittest import mock

from webapp import server


class _FakeResp:
    """Minimal stand-in for http.client.HTTPResponse used as a context manager by proxy_fetch."""

    def __init__(self, status, ctype, body):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProxyFetchTests(unittest.TestCase):
    """The chat is the single entry: it reverse-proxies GETs to the retrieval service. Lock the
    relay of status/content-type/body and the graceful 502 when the upstream is down."""

    def test_relays_success(self):
        with mock.patch("webapp.server.urllib.request.urlopen",
                        return_value=_FakeResp(200, "text/html; charset=utf-8", b"<html>ok</html>")):
            status, ctype, body = server.proxy_fetch("http://127.0.0.1:8848/arch.html")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertEqual(body, b"<html>ok</html>")

    def test_relays_http_error(self):
        err = urllib.error.HTTPError(
            "http://127.0.0.1:8848/impact", 404, "not found",
            {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"unknown route"}'))
        with mock.patch("webapp.server.urllib.request.urlopen", side_effect=err):
            status, ctype, body = server.proxy_fetch("http://127.0.0.1:8848/impact")
        self.assertEqual(status, 404)
        self.assertIn(b"error", body)

    def test_dead_upstream_is_502_with_hint(self):
        with mock.patch("webapp.server.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("Connection refused")):
            status, ctype, body = server.proxy_fetch("http://127.0.0.1:8848/repo-tags")
        self.assertEqual(status, 502)
        payload = json.loads(body)
        self.assertIn("unavailable", payload["error"])
        self.assertIn("retrieval_service.py", payload["hint"])


if __name__ == "__main__":
    unittest.main()

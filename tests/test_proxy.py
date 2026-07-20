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


def _handler(cookie=""):
    """A Handler with no real socket -- __new__ skips BaseHTTPRequestHandler.__init__ (which needs a
    live connection), and _resolve_uid only ever touches self.headers."""
    handler = server.Handler.__new__(server.Handler)
    handler.headers = {"Cookie": cookie} if cookie else {}
    return handler


class ResolveUidTests(unittest.TestCase):
    """RUNBOOK-43: session/feedback isolation is keyed off an opaque per-browser id issued via
    cookie, separate from the LLM-routing pairing token. Lock the issuance + reuse behavior."""

    def test_first_visit_issues_a_new_uid(self):
        handler = _handler()
        handler._resolve_uid()
        self.assertTrue(handler._uid)
        self.assertEqual(handler._new_uid, handler._uid)  # _send must mint the Set-Cookie

    def test_returning_visit_reuses_the_cookie_and_does_not_reissue(self):
        handler = _handler(cookie="sdlc_uid=abc123; other=1")
        handler._resolve_uid()
        self.assertEqual(handler._uid, "abc123")
        self.assertIsNone(handler._new_uid)  # already has one -> no Set-Cookie on this response

    def test_two_visits_get_different_uids(self):
        first, second = _handler(), _handler()
        first._resolve_uid()
        second._resolve_uid()
        self.assertNotEqual(first._uid, second._uid)


if __name__ == "__main__":
    unittest.main()

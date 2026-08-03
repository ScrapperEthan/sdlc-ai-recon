"""The page's stylesheet and script live in their own files now; keep them reachable and whole.

`index.html` used to be 3641 lines of markup, CSS and JS in one file. Splitting it out is only safe
if two things stay true, and neither is obvious from reading any one file:

* the page still LINKS both assets, and the server still SERVES them at those exact paths. A
  stylesheet that 404s is a page that renders as unstyled text, which no unit test elsewhere notices;
* the script still loads BEFORE the mermaid bundle and the init snippet that follows it. That order
  was load-bearing when everything was inline and it is easier to break now that it spans files.

The route is checked at the handler level rather than by driving a browser, because what can regress
is the routing table, and that is what this reads.
"""
import os
import unittest
from unittest import mock

from webapp import server

STATIC = os.path.join(os.path.dirname(os.path.abspath(server.__file__)), "static")


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


class _Recorder:
    """Captures what the handler wrote, without a socket."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""


def _serve(path):
    """Run Handler.do_GET for `path` against a handler with everything stubbed but the routing."""
    handler = server.Handler.__new__(server.Handler)
    record = _Recorder()
    handler.path = path
    handler._uid, handler._new_uid = "test-uid", None
    handler.send_response = lambda code, *a: setattr(record, "status", code)
    handler.send_header = lambda name, value: record.headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    handler.wfile = mock.Mock()
    handler.wfile.write = lambda data: setattr(record, "body", data)
    handler._send = lambda status, body, ctype="": (
        setattr(record, "status", status), setattr(record, "body", body))
    with mock.patch.object(server.Handler, "_resolve_uid", lambda self: None):
        handler.do_GET()
    return record


class ServedTests(unittest.TestCase):

    def test_both_assets_are_served_with_the_right_content_type(self):
        for path, kind in (("/static/app.css", "text/css"),
                           ("/static/app.js", "application/javascript")):
            with self.subTest(path=path):
                record = _serve(path)
                self.assertEqual(record.status, 200)
                self.assertIn(kind, record.headers["Content-Type"])
                self.assertGreater(len(record.body), 1000)

    def test_assets_are_not_cached_by_the_browser(self):
        """An internal tool people edit and reload. A stale app.js that "did not pick up my change"
        costs more than the bytes a cache header would save."""
        self.assertEqual(_serve("/static/app.js").headers.get("Cache-Control"), "no-cache")

    def test_the_request_path_cannot_reach_outside_the_static_directory(self):
        """Served from a fixed MAP, so a request path never contributes to a filesystem path.

        These do not 404 — an unmatched GET falls through to the reverse proxy and comes back 502
        with the upstream down, which is the pre-existing behaviour for every unknown path. What
        matters is that none of them reads a file off this box: the traversal is not blocked by a
        check that could be removed, it is unexpressible, because the only paths that open a file
        are the two literal keys.
        """
        for path in ("/static/../server.py", "/static/app.js/../../config.py",
                     "/static/%2e%2e/server.py", "/static/../../../etc/passwd"):
            with self.subTest(path=path):
                self.assertNotIn(path, server.STATIC_FILES)
                body = _serve(path).body
                text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
                self.assertNotIn("import os", text)          # no Python source came back
                self.assertNotIn("class Handler", text)


class PageWiringTests(unittest.TestCase):

    def test_index_links_both_assets(self):
        html = _read("index.html")
        self.assertIn('<link rel="stylesheet" href="/static/app.css">', html)
        self.assertIn('<script src="/static/app.js"></script>', html)

    def test_nothing_was_left_behind_inline(self):
        """A stray inline block would silently win the cascade over the extracted file."""
        html = _read("index.html")
        self.assertNotIn("<style>", html)

    def test_the_script_still_loads_before_mermaid_and_its_init(self):
        html = _read("index.html")
        self.assertLess(html.index("/static/app.js"), html.index("mermaid.min.js"))
        # `window.mermaid.initialize`, not `window.mermaid` — the latter also appears in the comment
        # above the vendor tag, so anchoring on it measured the comment's position, not the code's.
        self.assertLess(html.index("mermaid.min.js"), html.index("window.mermaid.initialize"))

    def test_the_script_runs_after_the_markup_it_reads_at_load_time(self):
        """It calls getElementById at top level. As a classic script at the end of the body that is
        fine; hoisted into <head> it would bind every one of those to null."""
        html = _read("index.html")
        self.assertLess(html.index('id="mcp-panel"'), html.index("/static/app.js"))
        self.assertLess(html.index('id="q"'), html.index("/static/app.js"))

    def test_the_stylesheet_kept_both_original_blocks(self):
        """Concatenated in their original order, so the cascade is unchanged — a rule from the
        second block still wins a tie against the first."""
        css = _read("app.css")
        self.assertIn(".app-shell", css)        # first block
        self.assertIn(".mcp-probe", css)        # second block
        self.assertLess(css.index(".app-shell"), css.index(".mcp-probe"))


if __name__ == "__main__":
    unittest.main()

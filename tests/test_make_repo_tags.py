import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from unittest import mock

import make_repo_tags
import retrieval_service
from retriever import config as rconfig


EDGES = """from_repo,to_repo
mc-hk-hase-svc-rt-alert-sms-api,mc-hk-hase-api-parent
amet-mdc-hsbc-batch-email-job,mc-hk-hase-api-parent
"""

BUNDLES = {
    "ingress": {"primary": ["mc-hk-hase-svc-rt-alert-sms-api"], "with_libs": ["mc-hk-hase-svc-rt-alert-sms-api"]},
    "email-batch": {"primary": ["amet-mdc-hsbc-batch-email-job"], "with_libs": ["amet-mdc-hsbc-batch-email-job"]},
}

ROUTE_TAGS = {
    "mc-hk-hase-svc-rt-alert-sms-api": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "realtime",
        "tokens": ["svc", "alert"],
        "bundle": "ingress",
    },
    "amet-mdc-hsbc-batch-email-job": {
        "system": "amet-mdc",
        "channel": ["email"],
        "mode": "batch",
        "tokens": ["hsbc"],
        "bundle": "email-batch",
    },
}


class MakeRepoTagsTests(unittest.TestCase):
    def _request_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))

    def _write_service_root(self, root, with_tags=True):
        index_dir = os.path.join(root, "index")
        os.makedirs(index_dir, exist_ok=True)
        if with_tags:
            with open(os.path.join(index_dir, "repo_tags.json"), "w", encoding="utf-8") as handle:
                json.dump(ROUTE_TAGS, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")

    def test_derivation_assigns_system_mode_channel_and_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            bundles = os.path.join(tmp, "bundles.json")
            override = os.path.join(tmp, "override.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump(BUNDLES, handle)
            with open(override, "w", encoding="utf-8") as handle:
                json.dump({}, handle)

            args = make_repo_tags.parse_args(
                ["--edges", edges, "--bundles", bundles, "--override", override, "--out", os.path.join(tmp, "out.json")]
            )
            payload = make_repo_tags.build_repo_tags(args)

            entry = payload["mc-hk-hase-svc-rt-alert-sms-api"]
            self.assertEqual(entry["system"], "hase")
            self.assertEqual(entry["mode"], "realtime")
            self.assertEqual(entry["channel"], ["sms"])
            self.assertEqual(entry["bundle"], "ingress")

    def test_override_merge_wins_over_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            bundles = os.path.join(tmp, "bundles.json")
            override = os.path.join(tmp, "override.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump(BUNDLES, handle)
            with open(override, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "mc-hk-hase-svc-rt-alert-sms-api": {
                            "channel": ["wechat"],
                            "mode": "batch",
                            "bundle": "manual-ingress",
                        }
                    },
                    handle,
                )

            args = make_repo_tags.parse_args(
                ["--edges", edges, "--bundles", bundles, "--override", override, "--out", os.path.join(tmp, "out.json")]
            )
            payload = make_repo_tags.build_repo_tags(args)

            entry = payload["mc-hk-hase-svc-rt-alert-sms-api"]
            self.assertEqual(entry["channel"], ["wechat"])
            self.assertEqual(entry["mode"], "batch")
            self.assertEqual(entry["bundle"], "manual-ingress")

    def test_repos_route_filters_and_missing_file_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_service_root(tmp, with_tags=True)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))

                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    status, payload = self._request_json(
                        f"http://{host}:{port}/repos?channel=sms&mode=realtime&system=hase&bundle=ingress"
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["repos"], ["mc-hk-hase-svc-rt-alert-sms-api"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

        with tempfile.TemporaryDirectory() as tmp:
            self._write_service_root(tmp, with_tags=False)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))

                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"http://{host}:{port}/repos?channel=sms", timeout=5)
                    self.assertEqual(caught.exception.code, 404)
                    payload = json.loads(caught.exception.read().decode("utf-8"))
                    self.assertEqual(payload["error"], "no repo_tags.json")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

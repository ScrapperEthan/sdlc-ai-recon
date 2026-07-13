import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import ExitStack
from unittest import mock

import make_arch_map
import retrieval_service
from retriever import config as rconfig


# A small, hermetic node catalog — not the real static/arch_nodes.json — so the binding
# rules are exercised in isolation.
CATALOG = {
    "nodes": [
        {"id": "sms-deli", "label": "SMS 投递任务", "column": 4, "slot": 1,
         "role": "delivery-job", "channel": "sms"},
        {"id": "sms-sinch", "label": "Sinch", "column": 5, "slot": 1,
         "role": "outbound-api", "channel": "sms", "vendor": "sinch"},
        {"id": "ingress-api", "label": "Ingress", "column": 1, "slot": 1, "role": "ingress"},
        {"id": "wechat-deli", "label": "WeChat 投递任务", "column": 4, "slot": 7,
         "role": "delivery-job", "channel": "wechat"},
        {"id": "whatsapp-haro", "label": "HASE HARO", "column": 5, "slot": 6,
         "role": "outbound-api", "channel": "whatsapp", "vendor": "haro",
         "note": "⚠ 名称不含 whatsapp，需人工 override"},
    ]
}

TOPOLOGY = {
    "sms": {
        "sinch": {
            "delivery_jobs": [{"repo": "mc-x-svc-bat-sinch-sms-deli-job", "channel": "sms", "vendor": "sinch"}],
            "outbound_apis": [{"repo": "mc-x-sinch-outbound-api", "vendor": "sinch"}],
        },
        "csl": {
            "delivery_jobs": [{"repo": "mc-x-svc-bat-csl-sms-deli-job", "channel": "sms", "vendor": "csl"}],
            "outbound_apis": [{"repo": "mc-x-csl-outbound-api", "vendor": "csl"}],
        },
    },
    "by_repo": {},
}

TAGS = {
    "mc-x-svc-bat-sinch-sms-deli-job": {"channel": ["sms"], "serves_channels": ["sms"]},
    "mc-x-svc-bat-csl-sms-deli-job": {"channel": ["sms"], "serves_channels": ["sms"]},
    "mc-x-sinch-outbound-api": {"channel": [], "serves_channels": ["sms"]},
    # "others" is a sheet bucket — it must never survive into a node's serves_channels rollup.
    "mc-hk-hase-ingress-api": {"channel": ["others"], "serves_channels": ["sms", "others"]},
}


class MakeArchMapTests(unittest.TestCase):
    def test_delivery_job_binds_all_vendor_jobs_for_channel(self):
        nodes = make_arch_map.build_map(CATALOG, TOPOLOGY, TAGS)
        sms = nodes["sms-deli"]
        self.assertEqual(
            sms["repos"],
            ["mc-x-svc-bat-csl-sms-deli-job", "mc-x-svc-bat-sinch-sms-deli-job"],
        )
        self.assertEqual(sms["repo_count"], 2)
        self.assertTrue(sms["bound"])
        self.assertEqual(sms["serves_channels"], ["sms"])

    def test_outbound_api_binds_only_its_vendor(self):
        nodes = make_arch_map.build_map(CATALOG, TOPOLOGY, TAGS)
        self.assertEqual(nodes["sms-sinch"]["repos"], ["mc-x-sinch-outbound-api"])

    def test_ingress_binds_by_name_and_others_never_a_channel(self):
        nodes = make_arch_map.build_map(CATALOG, TOPOLOGY, TAGS)
        ingress = nodes["ingress-api"]
        self.assertEqual(ingress["repos"], ["mc-hk-hase-ingress-api"])
        # "others" from the bound repo's tags must be scrubbed from the rollup.
        self.assertEqual(ingress["serves_channels"], ["sms"])
        self.assertNotIn("others", ingress["serves_channels"])

    def test_unbound_node_is_empty_and_honest(self):
        nodes = make_arch_map.build_map(CATALOG, TOPOLOGY, TAGS)
        wechat = nodes["wechat-deli"]
        self.assertEqual(wechat["repos"], [])
        self.assertEqual(wechat["repo_count"], 0)
        self.assertFalse(wechat["bound"])
        # A name-opaque node (HARO) also stays empty until an override fills it — note preserved.
        haro = nodes["whatsapp-haro"]
        self.assertFalse(haro["bound"])
        self.assertIn("override", haro["note"])

    def test_override_fills_name_opaque_node(self):
        override = {
            "whatsapp-haro": {
                "repos": ["mc-hk-hase-haro-svc"],
                "serves_channels": ["whatsapp"],
                "note": "bound by hand",
            }
        }
        nodes = make_arch_map.build_map(CATALOG, TOPOLOGY, TAGS, override)
        haro = nodes["whatsapp-haro"]
        self.assertEqual(haro["repos"], ["mc-hk-hase-haro-svc"])
        self.assertTrue(haro["bound"])
        self.assertIn("whatsapp", haro["serves_channels"])
        self.assertIn("override", haro["sources"])
        self.assertEqual(haro["note"], "bound by hand")

    def test_coverage_counts_bound_and_empty(self):
        nodes = make_arch_map.build_map(CATALOG, TOPOLOGY, TAGS)
        rows = dict(make_arch_map.coverage(nodes))
        self.assertEqual(rows["nodes_total"], 5)
        self.assertEqual(rows["nodes_bound"], 3)
        self.assertEqual(rows["nodes_empty"], 2)

    def test_build_payload_reads_files_and_wraps_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = os.path.join(tmp, "arch_nodes.json")
            topo_path = os.path.join(tmp, "delivery_topology.json")
            tags_path = os.path.join(tmp, "repo_tags.json")
            override_path = os.path.join(tmp, "arch_map.override.json")
            for path, data in (
                (catalog_path, CATALOG), (topo_path, TOPOLOGY), (tags_path, TAGS),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)

            payload = make_arch_map.build_payload(catalog_path, topo_path, tags_path, override_path)
            self.assertIn("generated_at", payload)
            self.assertEqual(payload["coverage"]["nodes_bound"], 3)
            self.assertEqual(payload["nodes"]["sms-deli"]["repo_count"], 2)

            out = os.path.join(tmp, "arch_map.json")
            make_arch_map.write_payload(payload, out)
            with open(out, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["nodes"]["sms-deli"]["repo_count"], 2)


class ArchMapServiceTests(unittest.TestCase):
    def _request_raw(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), response.info(), response.read().decode("utf-8")

    def _request_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))

    def _serve(self, arch_map_path):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(rconfig, "ARCH_MAP_JSON", arch_map_path))
        server = retrieval_service.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]

        def teardown():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            stack.close()

        return f"http://{host}:{port}", teardown

    def test_arch_html_served_as_html(self):
        base, teardown = self._serve(os.path.join(tempfile.gettempdir(), "does-not-exist.json"))
        try:
            status, headers, body = self._request_raw(f"{base}/arch.html")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_content_type(), "text/html")
            self.assertIn("MDC 通知管线", body)
        finally:
            teardown()

    def test_arch_map_absent_returns_clean_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, teardown = self._serve(os.path.join(tmp, "arch_map.json"))
            try:
                status, payload = self._request_json(f"{base}/arch-map")
                self.assertEqual(status, 200)
                self.assertEqual(payload["nodes"], {})
                self.assertIn("note", payload)
            finally:
                teardown()

    def test_arch_map_present_returns_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch_map_path = os.path.join(tmp, "arch_map.json")
            payload = {
                "generated_at": "2026-07-13T00:00:00Z",
                "nodes": {"sms-deli": {"repos": ["a"], "repo_count": 1, "serves_channels": ["sms"], "bound": True}},
                "coverage": {"nodes_total": 1, "nodes_bound": 1, "nodes_empty": 0},
            }
            with open(arch_map_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            base, teardown = self._serve(arch_map_path)
            try:
                status, got = self._request_json(f"{base}/arch-map")
                self.assertEqual(status, 200)
                self.assertEqual(got["nodes"]["sms-deli"]["repo_count"], 1)
                self.assertEqual(got["coverage"]["nodes_bound"], 1)
            finally:
                teardown()


if __name__ == "__main__":
    unittest.main()

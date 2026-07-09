import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import ExitStack
from unittest import mock

import impact_report
import retrieval_service
from retriever import config as rconfig


EDGES = """from_repo,to_repo
mc-hk-hase-web-api,mc-hk-hase-svc-rt-alert-sms-api
mc-hk-hase-bat-notify-job,mc-hk-hase-web-api
mc-hk-hase-svc-rt-alert-sms-api,mc-hk-hase-core-lib
"""

MESSAGES = """producer_repo,destination,consumer_repo,routing_source,evidence
mc-hk-hase-svc-rt-alert-sms-api,alerts.sms.topic,mc-hk-hase-tracking-job,annotation,src/main/java/AlertPublisher.java:10
mc-hk-hase-other-api,alerts.sms.topic,mc-hk-hase-web-api,annotation,src/main/java/AlertConsumer.java:20
"""

USECASE = """use_case_id,topic
UC123,alerts.sms.topic
"""

REPO_TAGS = {
    "mc-hk-hase-bat-notify-job": {
        "system": "hase",
        "channel": [],
        "mode": "batch",
        "tokens": ["notify"],
        "bundle": "notify",
    },
    "mc-hk-hase-core-lib": {
        "system": "hase",
        "channel": [],
        "mode": "lib",
        "tokens": ["core"],
        "bundle": "platform-core",
    },
    "mc-hk-hase-other-api": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "api",
        "tokens": ["other"],
        "bundle": "alerts",
    },
    "mc-hk-hase-svc-rt-alert-sms-api": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "realtime",
        "tokens": ["svc", "alert"],
        "bundle": "alerts",
    },
    "mc-hk-hase-tracking-job": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "job",
        "tokens": ["tracking"],
        "bundle": "tracking",
    },
    "mc-hk-hase-web-api": {
        "system": "hase",
        "channel": [],
        "mode": "api",
        "tokens": ["web"],
        "bundle": "alerts",
    },
}

GLOSSARY = {"svc": "servicing", "rt": "realtime"}


class ImpactReportTests(unittest.TestCase):
    def _write_fixture_root(self, root):
        recon_dir = os.path.join(root, "recon_out")
        index_dir = os.path.join(root, "index")
        os.makedirs(recon_dir, exist_ok=True)
        os.makedirs(index_dir, exist_ok=True)

        with open(os.path.join(recon_dir, "internal_edges.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write(EDGES)
        with open(os.path.join(index_dir, "message_edges.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write(MESSAGES)
        with open(
            os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as handle:
            handle.write(USECASE)
        with open(os.path.join(index_dir, "repo_tags.json"), "w", encoding="utf-8") as handle:
            json.dump(REPO_TAGS, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        with open(os.path.join(index_dir, "glossary.json"), "w", encoding="utf-8") as handle:
            json.dump(GLOSSARY, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        with open(os.path.join(index_dir, "REPOMAP.md"), "w", encoding="utf-8") as handle:
            handle.write("## mc-hk-hase-svc-rt-alert-sms-api\n")

    def _request_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))

    def test_repo_target_lists_upstream_and_downstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "RECON_DIR", os.path.join(tmp, "recon_out")))
                stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(tmp, "recon_out", "internal_edges.csv")))
                stack.enter_context(
                    mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(tmp, "index", "message_edges.csv"))
                )
                stack.enter_context(
                    mock.patch.object(
                        rconfig,
                        "USECASE_SNAPSHOT_CSV",
                        os.path.join(tmp, "index", "tbl_event_router_usecase_topic.snapshot.csv"),
                    )
                )
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))
                stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(tmp, "index", "glossary.json")))

                report = impact_report.build_report("mc-hk-hase-svc-rt-alert-sms-api")

                self.assertEqual([item["repo"] for item in report["upstream"]], ["mc-hk-hase-core-lib"])
                self.assertEqual(
                    [item["repo"] for item in report["downstream"]],
                    ["mc-hk-hase-bat-notify-job", "mc-hk-hase-web-api"],
                )
                self.assertIn("svc=servicing", report["target"]["description"])
                self.assertIn("rt=realtime", report["target"]["description"])

    def test_topic_target_lists_producers_and_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "RECON_DIR", os.path.join(tmp, "recon_out")))
                stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(tmp, "recon_out", "internal_edges.csv")))
                stack.enter_context(
                    mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(tmp, "index", "message_edges.csv"))
                )
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))
                stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(tmp, "index", "glossary.json")))

                report = impact_report.build_report("topic:alerts.sms.topic")

                self.assertEqual(len(report["async_routes"]), 1)
                route = report["async_routes"][0]
                self.assertEqual(route["producers"], ["mc-hk-hase-other-api", "mc-hk-hase-svc-rt-alert-sms-api"])
                self.assertEqual(route["consumers"], ["mc-hk-hase-tracking-job", "mc-hk-hase-web-api"])

    def test_unknown_target_returns_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "RECON_DIR", os.path.join(tmp, "recon_out")))
                stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(tmp, "recon_out", "internal_edges.csv")))
                stack.enter_context(
                    mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(tmp, "index", "message_edges.csv"))
                )
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))
                stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(tmp, "index", "glossary.json")))

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = impact_report.main(["topic:missing"])
                self.assertEqual(exit_code, 1)
                self.assertIn("unknown target", stdout.getvalue())

    def test_http_endpoint_matches_report_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "RECON_DIR", os.path.join(tmp, "recon_out")))
                stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(tmp, "recon_out", "internal_edges.csv")))
                stack.enter_context(
                    mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(tmp, "index", "message_edges.csv"))
                )
                stack.enter_context(
                    mock.patch.object(
                        rconfig,
                        "USECASE_SNAPSHOT_CSV",
                        os.path.join(tmp, "index", "tbl_event_router_usecase_topic.snapshot.csv"),
                    )
                )
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))
                stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(tmp, "index", "glossary.json")))

                expected = impact_report.build_report("mc-hk-hase-svc-rt-alert-sms-api")

                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    status, payload = self._request_json(
                        f"http://{host}:{port}/impact-report?target=mc-hk-hase-svc-rt-alert-sms-api"
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["target"], expected["target"])
                    self.assertEqual(payload["upstream"], expected["upstream"])
                    self.assertEqual(payload["downstream"], expected["downstream"])
                    self.assertEqual(payload["async_routes"], expected["async_routes"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

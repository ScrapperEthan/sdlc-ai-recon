import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from unittest import mock

import retrieval_service
from retriever import config as rconfig


EDGES = """from_repo,to_repo
mc-hk-hase-ingress-api,mc-hk-hase-api-parent
mc-hk-hase-ingress-job,mc-hk-hase-ingress-api
"""

MESSAGES = """producer_repo,destination,consumer_repo,routing_source,evidence
mc-hk-hase-ingress-api,orders.topic,mc-hk-hase-tracking-job,annotation,src/main/java/Foo.java:10
mc-hk-hase-ingress-api,sms.alert.topic,mc-hk-hase-notify-job,annotation,src/main/java/Bar.java:12
"""

REPO_TAGS = {
    "mc-hk-hase-ingress-api": {
        "system": "hase",
        "channel": ["sms", "email"],
        "mode": "realtime",
        "tokens": ["ingress"],
        "bundle": "cards",
    },
    "mc-hk-hase-notify-job": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "batch",
        "tokens": ["notify"],
        "bundle": "cards",
    },
}

GLOSSARY = {
    "mc": "Mastercard",
    "hk": "Hong Kong",
    "hase": "HASE",
}

USECASE = """use_case_id,topic
cancel-card,sms.alert.topic
"""


class RetrievalServiceTests(unittest.TestCase):
    def _write_fixture_root(self, root):
        recon_dir = os.path.join(root, "recon_out")
        index_dir = os.path.join(root, "index")
        mirror_dir = os.path.join(root, "mirror", "mc-hk-hase-ingress-api", "src", "main", "java")
        os.makedirs(recon_dir, exist_ok=True)
        os.makedirs(index_dir, exist_ok=True)
        os.makedirs(mirror_dir, exist_ok=True)

        with open(os.path.join(recon_dir, "internal_edges.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write(EDGES)
        with open(os.path.join(index_dir, "message_edges.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write(MESSAGES)
        with open(os.path.join(index_dir, "REPOMAP.md"), "w", encoding="utf-8") as handle:
            handle.write("# repomap\n")
        with open(os.path.join(index_dir, "last_indexed.json"), "w", encoding="utf-8") as handle:
            json.dump({"generated_at": "2026-07-08T00:00:00Z"}, handle)
        with open(os.path.join(index_dir, "repo_tags.json"), "w", encoding="utf-8") as handle:
            json.dump(REPO_TAGS, handle)
        with open(os.path.join(index_dir, "glossary.json"), "w", encoding="utf-8") as handle:
            json.dump(GLOSSARY, handle)
        with open(
            os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"),
            "w",
            encoding="utf-8",
            newline="",
        ) as handle:
            handle.write(USECASE)
        with open(os.path.join(mirror_dir, "IngressResource.java"), "w", encoding="utf-8") as handle:
            handle.write("class IngressResource {}\n")

    def _request_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))

    def _request_raw(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), response.info(), response.read().decode("utf-8")

    def test_http_routes_match_fixture_graph_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "MIRROR", os.path.join(tmp, "mirror")))
                stack.enter_context(mock.patch.object(rconfig, "RECON_DIR", os.path.join(tmp, "recon_out")))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(
                    mock.patch.object(rconfig, "EDGES_CSV", os.path.join(tmp, "recon_out", "internal_edges.csv"))
                )
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
                stack.enter_context(
                    mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(tmp, "index", "glossary.json"))
                )
                stack.enter_context(
                    mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json"))
                )

                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    base = f"http://{host}:{port}"

                    status, health = self._request_json(f"{base}/health")
                    self.assertEqual(status, 200)
                    self.assertEqual(health["indexed_as_of"], "2026-07-08T00:00:00Z")

                    status, headers, html = self._request_raw(f"{base}/")
                    self.assertEqual(status, 200)
                    self.assertEqual(headers.get_content_type(), "text/html")
                    self.assertIn("影响分析演示台", html)

                    status, impact = self._request_json(
                        f"{base}/impact?repo=mc-hk-hase-ingress-api&transitive=1"
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(impact["repo"], "mc-hk-hase-ingress-api")
                    self.assertEqual(impact["depended_on_by"], ["mc-hk-hase-ingress-job"])

                    status, report = self._request_json(f"{base}/impact-report?target=mc-hk-hase-ingress-api")
                    self.assertEqual(status, 200)
                    self.assertEqual(report["target"]["kind"], "repo")
                    self.assertIn("sms", report["target"]["channels"])
                    self.assertGreaterEqual(len(report["async_routes"]), 2)

                    status, usecase_report = self._request_json(f"{base}/impact-report?target=use-case:cancel-card")
                    self.assertEqual(status, 200)
                    self.assertEqual(usecase_report["target"]["kind"], "use-case")
                    self.assertEqual(usecase_report["target"]["matched_topics"], ["sms.alert.topic"])
                    self.assertTrue(
                        any(item["type"] == "honesty" for item in usecase_report["risk_callouts"])
                    )

                    status, topic_report = self._request_json(f"{base}/impact-report?target=topic:sms.alert.topic")
                    self.assertEqual(status, 200)
                    self.assertEqual(topic_report["target"]["kind"], "topic")
                    self.assertIn("sms", topic_report["target"]["channels"])

                    status, tagged = self._request_json(f"{base}/repos?channel=sms")
                    self.assertEqual(status, 200)
                    self.assertEqual(tagged["count"], 2)
                    self.assertIn("mc-hk-hase-ingress-api", tagged["repos"])

                    status, consumers = self._request_json(f"{base}/consumers?destination=orders.topic")
                    self.assertEqual(status, 200)
                    self.assertEqual(consumers[0]["consumer_repo"], "mc-hk-hase-tracking-job")

                    status, trace = self._request_json(f"{base}/trace?destination=orders.topic")
                    self.assertEqual(status, 200)
                    self.assertEqual(trace["steps"][0]["destination"], "orders.topic")

                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(
                            f"{base}/impact?repo=mc-hk-hase-missing-api",
                            timeout=5,
                        )
                    self.assertEqual(caught.exception.code, 404)
                    error = json.loads(caught.exception.read().decode("utf-8"))
                    self.assertIn("unknown repo", error["error"])
                    caught.exception.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

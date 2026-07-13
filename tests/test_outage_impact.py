import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from unittest import mock

import make_delivery_topology
import outage_report
import retrieval_service
from retriever import config as rconfig


EDGES = """from_repo,to_repo
mc-x-ingress-api,mc-x-svc-bat-sinch-sms-deli-job
mc-x-svc-bat-sinch-sms-deli-job,mc-x-sinch-outbound-api
mc-x-svc-bat-csl-sms-deli-job,mc-x-csl-outbound-api
"""

USECASE = """use_case_id,topic
UC-SINCH,mc_x_svc_bat_sms
UC-SMS-ONLY,mc_x_other_rt_sms
"""


class OutageImpactTests(unittest.TestCase):
    def _write_fixture_root(self, root):
        recon_dir = os.path.join(root, "recon_out")
        index_dir = os.path.join(root, "index")
        os.makedirs(recon_dir, exist_ok=True)
        os.makedirs(index_dir, exist_ok=True)
        with open(os.path.join(recon_dir, "internal_edges.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write(EDGES)
        with open(os.path.join(index_dir, "message_edges.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write("producer_repo,destination,consumer_repo,routing_source,evidence\n")
        with open(os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"), "w", encoding="utf-8", newline="") as handle:
            handle.write(USECASE)

    def _patch_config(self, stack, root):
        index_dir = os.path.join(root, "index")
        recon_dir = os.path.join(root, "recon_out")
        stack.enter_context(mock.patch.object(rconfig, "ROOT", root))
        stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", index_dir))
        stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(recon_dir, "internal_edges.csv")))
        stack.enter_context(mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(index_dir, "message_edges.csv")))
        stack.enter_context(mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv")))
        stack.enter_context(mock.patch.object(rconfig, "DELIVERY_TOPOLOGY_JSON", os.path.join(index_dir, "delivery_topology.json")))
        stack.enter_context(mock.patch.object(rconfig, "DELIVERY_TOPOLOGY_OVERRIDE_JSON", os.path.join(index_dir, "delivery_topology.override.json")))
        # Isolate from the box's real repo_tags.json — build_topology merges its repos into
        # the universe, so without this the fixture leaks real *-deli-job repos.
        stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(index_dir, "repo_tags.json")))

    def _build_topology(self):
        payload, missing_jobs, missing_apis = make_delivery_topology.build_topology(
            rconfig.EDGES_CSV, rconfig.DELIVERY_TOPOLOGY_OVERRIDE_JSON, rconfig.REPO_TAGS_JSON
        )
        make_delivery_topology.write_payload(payload, rconfig.DELIVERY_TOPOLOGY_JSON)
        return payload, missing_jobs, missing_apis

    def _request_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))

    def test_topology_parses_delivery_vendor_and_outbound_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp)
                payload, missing_jobs, missing_apis = self._build_topology()
                job = payload["sms"]["sinch"]["delivery_jobs"][0]
                self.assertEqual(job["repo"], "mc-x-svc-bat-sinch-sms-deli-job")
                self.assertEqual(job["vendor"], "sinch")
                self.assertEqual(payload["sms"]["sinch"]["outbound_apis"][0]["repo"], "mc-x-sinch-outbound-api")
                self.assertFalse(missing_jobs)
                self.assertFalse(missing_apis)

    def test_channel_and_vendor_reports_have_expected_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp)
                self._build_topology()
                channel = outage_report.build_report("channel:sms")
                self.assertEqual(channel["confidence"], "high")
                self.assertEqual(channel["affected_use_cases"]["count"], 2)
                self.assertTrue(all(item["source"] == "channel-token" for item in channel["affected_topics"]))

                vendor = outage_report.build_report("vendor:sinch")
                self.assertEqual(vendor["confidence"], "heuristic")
                self.assertEqual(vendor["affected_use_cases"]["count"], 1)
                self.assertEqual(vendor["affected_topics"][0]["source"], "token-heuristic")
                self.assertIn("index/tbl_event_router_usecase_topic.snapshot.csv:2", vendor["citations"])

    def _write_repo_tags(self, root, payload):
        with open(os.path.join(root, "index", "repo_tags.json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_serves_channels_fold_into_affected_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            self._write_repo_tags(tmp, {
                # a serving library that owns no channel -> serves-channel (blast-radius)
                "lib": {"serves_channels": ["sms"]},
                # a repo that owns the channel but is not in the delivery topology -> channel-owner
                "sms-owner": {"channel": ["sms"]},
                # a delivery-job repo that also owns/serves the channel must keep its topology label
                "mc-x-svc-bat-sinch-sms-deli-job": {"channel": ["sms"], "serves_channels": ["sms"]},
                # "others" is a sheet bucket, never a channel -> must never be folded in
                "others-lib": {"serves_channels": ["others"]},
                # a library serving a different channel must not appear for channel:sms
                "email-lib": {"serves_channels": ["email"]},
            })
            with ExitStack() as stack:
                self._patch_config(stack, tmp)
                self._build_topology()
                report = outage_report.build_report("channel:sms")
                affected = report["affected_repos"]
                rows = {item["repo"]: item for item in affected["items"]}

                self.assertEqual(rows["lib"]["relation"], "serves-channel")
                self.assertEqual(rows["lib"]["citations"], ["index/repo_tags.json"])
                self.assertEqual(rows["sms-owner"]["relation"], "channel-owner")
                # topology wins the label — the deli-job repo stays delivery-job, not channel-owner
                self.assertEqual(rows["mc-x-svc-bat-sinch-sms-deli-job"]["relation"], "delivery-job")
                self.assertNotIn("others-lib", rows)
                self.assertNotIn("email-lib", rows)

                by_relation = affected["by_relation"]
                self.assertEqual(by_relation.get("serves-channel"), 1)
                self.assertEqual(by_relation.get("channel-owner"), 1)
                self.assertNotIn("others", by_relation)
                self.assertEqual(sum(by_relation.values()), affected["count"])

    def test_http_matches_cli_shape_and_unknown_target_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp)
                self._build_topology()
                expected = outage_report.build_report("vendor:sinch")
                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    status, payload = self._request_json(f"http://{host}:{port}/outage-impact?vendor=sinch")
                    self.assertEqual(status, 200)
                    self.assertEqual(payload, expected)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"http://{host}:{port}/outage-impact?vendor=missing", timeout=5)
                    self.assertEqual(caught.exception.code, 404)
                    self.assertIn("unknown vendor", caught.exception.read().decode("utf-8"))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

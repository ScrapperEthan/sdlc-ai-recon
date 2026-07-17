import os
import tempfile
import unittest
from unittest import mock

from retriever import config, messages

_SHARED = "hrn.hase.wpb.notification.marketing-batch-oeml"
_OTHER_PREFIX = "hrn.bn.hsbc.wpb.notification.marketing-batch-oeml"
_C9508 = "hrn.hase.wpb.notification.servicing-realtime-highrisk-lccm_shp_svc_rt_hr_ses"


class ReverseLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "tbl_event_router_usecase_topic.snapshot.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("use_case_id,topic_name\n")          # line 1 (header)
            handle.write(f"C9508,{_C9508}\n")                  # line 2
            handle.write(f"C1000,{_SHARED}\n")                 # line 3
            handle.write(f"C1001,{_SHARED}\n")                 # line 4
            handle.write(f"C1002,{_OTHER_PREFIX}\n")           # line 5
            handle.write(f"C1000,{_SHARED}\n")                 # line 6 (duplicate of line 3)
        self._patch = mock.patch.object(config, "USECASE_SNAPSHOT_CSV", path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_exact_unique_topic_returns_only_that_use_case(self):
        out = messages.reverse_lookup_use_cases(_C9508)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["items"][0]["use_case"], "C9508")
        self.assertTrue(out["items"][0]["citation"].endswith(":2"))
        self.assertFalse(out["source"]["production_verified"])
        self.assertEqual(out["source"]["environment"], "dev/SCT")

    def test_exact_shared_topic_lists_siblings_and_dedupes(self):
        out = messages.reverse_lookup_use_cases(_SHARED)
        cases = sorted(item["use_case"] for item in out["items"])
        self.assertEqual(cases, ["C1000", "C1001"])   # C1002 is a different prefix; C1000 deduped
        self.assertEqual(out["total"], 2)

    def test_substring_spans_distinct_topics(self):
        out = messages.reverse_lookup_use_cases("marketing-batch-oeml", exact=False)
        self.assertEqual(out["query"]["match"], "substring")
        self.assertEqual(out["distinct_topic_count"], 2)
        self.assertEqual(out["total"], 3)  # (C1000,shared),(C1001,shared),(C1002,other)
        self.assertIn(_SHARED, out["matched_topics"])

    def test_limit_paginates_without_dropping_total(self):
        out = messages.reverse_lookup_use_cases(_SHARED, limit=1)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["returned"], 1)
        self.assertTrue(out["truncated"])

    def test_pair_verification_mode_hints_at_reverse_tool(self):
        out = messages.usecase_route(use_case_id="C9508", topic=_C9508)
        self.assertEqual(out["mode"], "pair_verification")
        self.assertIn("use_cases_for_topic", out["hint"])

    def test_topic_only_route_is_flagged_substring(self):
        out = messages.usecase_route(topic="marketing-batch-oeml")
        self.assertEqual(out["mode"], "topic_to_use_cases")
        self.assertEqual(out["match"], "substring")


if __name__ == "__main__":
    unittest.main()

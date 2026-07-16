import unittest

from retriever import arch_focus

# A small catalog exercising the vendor-vs-channel rule (not the real static/arch_nodes.json).
NODES = [
    {"id": "sms-topics", "role": "topic", "channel": "sms"},
    {"id": "sms-deli", "role": "delivery-job", "channel": "sms"},
    {"id": "sms-csl", "role": "outbound-api", "channel": "sms", "vendor": "csl"},
    {"id": "sms-sinch", "role": "outbound-api", "channel": "sms", "vendor": "sinch"},
    {"id": "ext-sinch", "role": "external", "channel": "sms", "vendor": "sinch"},
    {"id": "ext-csl", "role": "external", "channel": "sms", "vendor": "csl"},
    {"id": "email-deli", "role": "delivery-job", "channel": "email"},
    {"id": "decision-job", "role": "decision"},
    {"id": "decision-topics", "role": "decision"},
]


class AffectedNodesTests(unittest.TestCase):
    def test_vendor_chain_excludes_other_vendors(self):
        hit = arch_focus.affected_nodes(NODES, "vendor", "sinch")
        # Sinch's own outbound + terminal, plus the SHARED upstream (topics + delivery job) + routers
        for node_id in ("sms-sinch", "ext-sinch", "sms-topics", "sms-deli", "decision-job", "decision-topics"):
            self.assertIn(node_id, hit)
        # other vendors' outbound/terminal are NOT pulled in by a single-vendor outage
        self.assertNotIn("sms-csl", hit)
        self.assertNotIn("ext-csl", hit)
        # nor an unrelated channel
        self.assertNotIn("email-deli", hit)

    def test_channel_covers_all_vendors(self):
        hit = arch_focus.affected_nodes(NODES, "channel", "sms")
        for node_id in ("sms-topics", "sms-deli", "sms-csl", "sms-sinch", "ext-sinch", "ext-csl", "decision-job"):
            self.assertIn(node_id, hit)
        self.assertNotIn("email-deli", hit)


class FocusTests(unittest.TestCase):
    def test_focus_real_catalog_vendor(self):
        result = arch_focus.focus("vendor", "sinch")
        self.assertTrue(result["ok"])
        self.assertEqual(result["highlight"], "vendor:sinch")
        self.assertEqual(result["view"], "arch")
        self.assertTrue(result["url"].startswith("/arch.html?embed=1&highlight=vendor:sinch"))
        self.assertIn("ext-sinch", result["affected_node_ids"])
        self.assertNotIn("sms-csl", result["affected_node_ids"])
        self.assertGreater(result["affected_node_count"], 0)

    def test_focus_real_catalog_channel(self):
        result = arch_focus.focus("channel", "sms")
        self.assertTrue(result["ok"])
        # the whole SMS lane is broader than a single vendor
        self.assertGreater(result["affected_node_count"], len(arch_focus.focus("vendor", "sinch")["affected_node_ids"]))

    def test_unknown_value_lists_options(self):
        result = arch_focus.focus("vendor", "nope")
        self.assertFalse(result["ok"])
        self.assertIn("vendors", result)
        self.assertIn("sinch", result["vendors"])

    def test_bad_kind_rejected(self):
        result = arch_focus.focus("repo", "x")
        self.assertFalse(result["ok"])
        self.assertIn("channels", result)
        self.assertIn("vendors", result)


if __name__ == "__main__":
    unittest.main()

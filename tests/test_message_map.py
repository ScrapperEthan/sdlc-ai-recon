import json
import os
import tempfile
import unittest

import make_message_map as mm


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class ChannelDerivationTests(unittest.TestCase):
    def test_channel_from_name_then_vendor(self):
        self.assertEqual(mm.channel_of("hrn.hsbc.wpb.notification.servicing-highrisk-csl_svc_rt_sms"), "sms")
        self.assertEqual(mm.channel_of("q_marketing-tracking-haro_mkt_rt_gen_whatsapp"), "whatsapp")
        self.assertEqual(mm.channel_of("TLXNCAR.SASP.CARS.HASE_SMS_REQ"), "sms")
        self.assertEqual(mm.channel_of("q_csl_tracking"), "sms")          # vendor token -> sms
        self.assertEqual(mm.channel_of("hrn.hase.all.notification.default-omni"), "")  # no channel


class ScanTests(unittest.TestCase):
    def _mirror(self, tmp):
        # A config-driven Kafka consumer (topic in application.yml).
        _write(
            os.path.join(tmp, "amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job", "src", "main", "resources", "application.yml"),
            "consumerInformationList:\n"
            "  - topicName: hrn.hsbc.wpb.notification.servicing-realtime-highrisk-csl_svc_rt_sms\n"
            "    consumerGroup: cg-mdc-11345084-1\n",
        )
        # A producer with a Java topic constant.
        _write(
            os.path.join(tmp, "mc-hk-hase-api-aws-client", "src", "main", "java", "AbstractEventProducer.java"),
            'public class AbstractEventProducer {\n'
            '  private static final String MKT_CM_OUTBOUND_SMS_TOPIC = "hrn.hsbc.wpb.notification.outbound.marketing-cm_sms";\n'
            '  void publishMessage() { kafkaProducer.send(key, eventConfig.getTopicName(), events); }\n'
            '}\n',
        )
        # A test file that must be ignored.
        _write(
            os.path.join(tmp, "mc-hk-hase-api-aws-client", "src", "test", "java", "FooTest.java"),
            'String t = "hrn.hsbc.wpb.notification.test-only_email";\n',
        )

    def test_build_extracts_channels_roles_and_ignores_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._mirror(tmp)
            repos = mm.build(tmp)
            channels = mm.to_channels(repos)

            consumer = channels["amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job"]
            self.assertEqual(consumer["channels"], ["sms"])
            self.assertEqual(consumer["destinations"][0]["role"], "consume")
            self.assertEqual(consumer["destinations"][0]["kind"], "topic")

            producer = channels["mc-hk-hase-api-aws-client"]
            self.assertEqual(producer["channels"], ["sms"])
            self.assertEqual(producer["destinations"][0]["role"], "produce")
            # the /test/ email topic must not leak in
            self.assertNotIn("email", producer["channels"])

    def test_edges_pair_producers_with_consumers_on_shared_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            # same topic, one producer + one consumer -> one paired edge
            _write(os.path.join(tmp, "prod", "application.yml"),
                   "producer:\n  topicName: hrn.x.notification.cm_sms\n  send: true\n")
            _write(os.path.join(tmp, "cons", "application.yml"),
                   "consumerInformationList:\n  - topicName: hrn.x.notification.cm_sms\n")
            repos = mm.build(tmp)
            edges = mm.to_edges(repos)
            paired = [e for e in edges if e["producer_repo"] and e["consumer_repo"]]
            self.assertEqual(len(paired), 1)
            self.assertEqual(paired[0]["destination"], "hrn.x.notification.cm_sms")

    def test_coverage_counts_newly_covered_channel_unknown_repos(self):
        channels = {"libx": {"channels": ["sms"], "destinations": []}}
        tags = {"libx": {"channel": []}}  # name-unknown, now covered by messaging
        rows = dict(mm.coverage_rows(channels, tags))
        self.assertEqual(rows["repos_with_channel_via_msg"], 1)
        self.assertEqual(rows["channel_unknown_now_covered"], 1)


if __name__ == "__main__":
    unittest.main()

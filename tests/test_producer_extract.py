import os
import tempfile
import unittest

import producer_extract as pe
import make_message_map as mm


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


_SMS = "hrn.hase.wpb.notification.marketing-cm_sms"
_LETTER = "hrn.hase.wpb.notification.letter-otx_letter"

_PRODUCER_JAVA = (
    "public class SmsEventProducer extends AbstractEventProducer {\n"
    "  private KafkaTemplate kafkaTemplate;\n"
    f'  private static final String SMS_TOPIC = "{_SMS}";\n'
    "  void a() { kafkaTemplate.send(SMS_TOPIC, event); }\n"
    "  void b() { kafkaTemplate.send(eventConfig.getTopicName(), event); }\n"
    f'  void c() {{ jmsTemplate.convertAndSend("{_LETTER}", payload); }}\n'
    "}\n"
)


def _by(records, key, value):
    return [r for r in records if r.get(key) == value]


class ProducerExtractTests(unittest.TestCase):
    def _repo(self, tmp):
        _write(os.path.join(tmp, "prod", "src", "main", "java", "SmsEventProducer.java"), _PRODUCER_JAVA)

    def test_wrapper_class_is_a_producer_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            records = pe.scan_repo("prod", os.path.join(tmp, "prod"))
        wrappers = [r for r in records if r["routing_source"] == "wrapper"]
        self.assertEqual(len(wrappers), 1)
        self.assertEqual(wrappers[0]["producer_type"], "wrapper:AbstractEventProducer")
        self.assertEqual(wrappers[0]["confidence"], "high")
        self.assertEqual(wrappers[0]["producer_symbol"], "SmsEventProducer")

    def test_constant_destination_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            records = pe.scan_repo("prod", os.path.join(tmp, "prod"))
        const = _by(records, "routing_source", "constant")
        self.assertEqual(len(const), 1)
        self.assertEqual(const[0]["destination"], _SMS)
        self.assertEqual(const[0]["resolution_status"], "resolved")
        self.assertEqual(const[0]["confidence"], "high")  # confirmed KafkaTemplate receiver

    def test_builder_destination_kept_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            records = pe.scan_repo("prod", os.path.join(tmp, "prod"))
        builder = _by(records, "routing_source", "builder")
        self.assertEqual(len(builder), 1)
        self.assertEqual(builder[0]["resolution_status"], "unresolved")
        self.assertEqual(builder[0]["confidence"], "high")  # confirmed receiver, dest just dynamic

    def test_literal_via_trusted_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            records = pe.scan_repo("prod", os.path.join(tmp, "prod"))
        literal = _by(records, "routing_source", "literal")
        self.assertEqual([r["destination"] for r in literal], [_LETTER])
        self.assertEqual(literal[0]["confidence"], "high")

    def test_generic_send_with_unknown_receiver_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "noise", "src", "main", "java", "Foo.java"),
                   "public class Foo {\n  void x(java.util.List list) { list.send(item); }\n}\n")
            records = pe.scan_repo("noise", os.path.join(tmp, "noise"))
        self.assertEqual(records, [])  # no wrapper, no framework receiver -> nothing

    def test_name_family_wrapper_is_medium(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "svc", "src", "main", "java", "AlertSendService.java"),
                   "public class AlertSendService {\n  void run() {}\n}\n")
            records = pe.scan_repo("svc", os.path.join(tmp, "svc"))
        wrappers = [r for r in records if r["routing_source"] == "wrapper"]
        self.assertEqual(len(wrappers), 1)
        self.assertEqual(wrappers[0]["producer_type"], "wrapper:name-family")
        self.assertEqual(wrappers[0]["confidence"], "medium")

    def test_test_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "prod", "src", "test", "java", "SmsEventProducer.java"), _PRODUCER_JAVA)
            records = pe.scan_repo("prod", os.path.join(tmp, "prod"))
        self.assertEqual(records, [])


class ProducerIntegrationTests(unittest.TestCase):
    def test_main_appends_producer_rows_and_pairs_with_consumer(self):
        import csv
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "prod", "src", "main", "java", "SmsEventProducer.java"), _PRODUCER_JAVA)
            _write(os.path.join(tmp, "cons", "src", "main", "resources", "application.yml"),
                   f"consumerInformationList:\n  - topicName: {_SMS}\n    consumerGroup: cg-1\n")
            edges_out = os.path.join(tmp, "message_edges.csv")
            channels_out = os.path.join(tmp, "message_channels.json")
            mm.main(["--mirror", tmp, "--edges-out", edges_out,
                     "--channels-out", channels_out, "--repo-tags", os.path.join(tmp, "none.json")])
            with open(edges_out, encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        # schema is additive: the new evidence columns exist
        self.assertIn("confidence", rows[0])
        self.assertIn("resolution_status", rows[0])
        # the constant-resolved producer edge paired with the yml consumer on the same topic
        paired = [r for r in rows
                  if r["producer_repo"] == "prod" and r["consumer_repo"] == "cons"
                  and r["destination"] == _SMS]
        self.assertTrue(paired, "expected a producer->consumer edge on the shared SMS topic")
        # the signature-aware pass contributes a constant-resolved edge (alongside any the old
        # config scan happens to catch when a literal sits next to the send)
        self.assertTrue(any(r["routing_source"] == "constant" for r in paired))
        self.assertTrue(any(r["confidence"] == "high" for r in paired))


if __name__ == "__main__":
    unittest.main()

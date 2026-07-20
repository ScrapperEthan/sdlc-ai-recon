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


_ORDER_TOPIC = "hrn.hase.wpb.notification.order-cm_push"
_ORDER_QUEUE = "hrn.hase.wpb.notification.order-otx_queue"

# @Value field (-> yaml) and a getter (-> in-repo constant), the two dominant real-mirror cases.
_ORDER_PRODUCER = (
    "public class OrderEventProducer extends AbstractEventProducer {\n"
    "  private KafkaTemplate kafkaTemplate;\n"
    "  private EventConfig eventConfig;\n"
    '  @Value("${notification.order.topic}") private String orderTopic;\n'
    "  void a() { kafkaTemplate.send(orderTopic, event); }\n"
    "  void b() { kafkaTemplate.send(eventConfig.getTopicName(), event); }\n"
    "}\n"
)
_EVENT_CONFIG = (
    "public class EventConfig {\n"
    f'  private static final String ORDER_TOPIC = "{_ORDER_TOPIC}";\n'
    "  public String getTopicName() { return ORDER_TOPIC; }\n"
    "}\n"
)
_ORDER_YML = f"notification:\n  order:\n    topic: {_ORDER_TOPIC}\n"

# A chained config getter: config.getQueue() -> getQueue() returns an @Value field -> .properties.
_QUEUE_CONFIG = (
    "public class QueueConfig {\n"
    '  @Value("${orders.queue.name}") private String queueName;\n'
    "  public String getQueue() { return queueName; }\n"
    "}\n"
)
_QUEUE_SENDER = (
    "public class OrderQueueSender {\n"
    "  private JmsTemplate jmsTemplate;\n"
    "  private QueueConfig config;\n"
    "  void run() { jmsTemplate.convertAndSend(config.getQueue(), payload); }\n"
    "}\n"
)
# A constant defined in a *different* file in the same repo (qualified reference).
_TOPICS = f'public class Topics {{\n  public static final String PUSH_TOPIC = "{_ORDER_TOPIC}";\n}}\n'
_PUSH_PRODUCER = (
    "public class PushProducer extends AbstractEventProducer {\n"
    "  private KafkaTemplate kafkaTemplate;\n"
    "  void s() { kafkaTemplate.send(Topics.PUSH_TOPIC, event); }\n"
    "}\n"
)


class ProducerResolutionTests(unittest.TestCase):
    """RUNBOOK-42: the send arg is rarely a literal; resolve it via the per-repo index."""

    def _scan(self, tmp, files):
        for rel, text in files.items():
            _write(os.path.join(tmp, "prod", rel), text)
        return pe.scan_repo("prod", os.path.join(tmp, "prod"))

    def _dest_for(self, records, symbol_substr):
        hits = [r for r in records if symbol_substr in r.get("destination_expression", "")]
        self.assertTrue(hits, f"no record whose expr contains {symbol_substr!r}")
        return hits[0]

    def test_value_field_resolves_through_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/OrderEventProducer.java": _ORDER_PRODUCER,
                "src/main/java/EventConfig.java": _EVENT_CONFIG,
                "src/main/resources/application.yml": _ORDER_YML,
            })
        rec = self._dest_for(records, "orderTopic")
        self.assertEqual(rec["destination"], _ORDER_TOPIC)
        self.assertEqual(rec["routing_source"], "config")
        self.assertEqual(rec["resolution_status"], "resolved")
        self.assertEqual(rec["confidence"], "high")  # confirmed KafkaTemplate receiver

    def test_getter_resolves_through_in_repo_constant(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/OrderEventProducer.java": _ORDER_PRODUCER,
                "src/main/java/EventConfig.java": _EVENT_CONFIG,
                "src/main/resources/application.yml": _ORDER_YML,
            })
        rec = self._dest_for(records, "getTopicName")
        self.assertEqual(rec["destination"], _ORDER_TOPIC)
        self.assertEqual(rec["routing_source"], "builder")
        self.assertEqual(rec["resolution_status"], "resolved")

    def test_chained_config_getter_resolves_through_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/QueueConfig.java": _QUEUE_CONFIG,
                "src/main/java/OrderQueueSender.java": _QUEUE_SENDER,
                "src/main/resources/application.properties": f"orders.queue.name={_ORDER_QUEUE}\n",
            })
        rec = self._dest_for(records, "getQueue")
        self.assertEqual(rec["destination"], _ORDER_QUEUE)
        self.assertEqual(rec["routing_source"], "builder")
        self.assertEqual(rec["resolution_status"], "resolved")

    def test_cross_file_qualified_constant_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/PushProducer.java": _PUSH_PRODUCER,
                "src/main/java/Topics.java": _TOPICS,
            })
        rec = self._dest_for(records, "PUSH_TOPIC")
        self.assertEqual(rec["destination"], _ORDER_TOPIC)
        self.assertEqual(rec["routing_source"], "constant")
        self.assertEqual(rec["resolution_status"], "resolved")

    def test_value_field_unresolved_without_yaml_stays_config_candidate(self):
        # @Value key present but no yaml to resolve it -> kept as a config candidate, not dropped
        # and not mislabeled runtime-unresolved.
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/OrderEventProducer.java": _ORDER_PRODUCER,
                "src/main/java/EventConfig.java": _EVENT_CONFIG,
            })
        rec = self._dest_for(records, "orderTopic")
        self.assertEqual(rec["resolution_status"], "unresolved")
        self.assertEqual(rec["routing_source"], "config")


# RUNBOOK-42 real-mirror re-verify (internal Codex, 2026-07-20): v2 produced a byte-identical
# message_edges.csv to v1 on the real mirror — zero new resolutions. Root causes it identified by
# hand: (1) getters generated by Lombok @Getter/@Data have no body in source, so the explicit-body
# getter regex never sees them (3/3 spot-checked real unresolved getters were plain Lombok fields);
# (2) a wrapper's own method declaration reusing a trusted method name (e.g. `publishMessage(...) {`)
# was being counted as a call site since the receiver-confirmation guard only covers generic
# send/publish; (3) the destination sits in a later argument, not always the first (`send(payload,
# eventConfig)`).
_MQ_TOPIC = "hrn.hase.wpb.notification.alerts-cm_mq"

_LOMBOK_VALUE_PRODUCER = (
    "@Getter\n"
    "public class AlertConfig {\n"
    '  @Value("${alerts.mq.topic}") private String queueName;\n'
    "}\n"
)
_LOMBOK_SENDER = (
    "public class AlertProducer extends AbstractEventProducer {\n"
    "  private KafkaTemplate kafkaTemplate;\n"
    "  private AlertConfig config;\n"
    "  void run() { kafkaTemplate.send(config.getQueueName(), payload); }\n"
    "}\n"
)

_PROPS_BOUND_CONFIG = (
    '@ConfigurationProperties(prefix = "alerts.mq")\n'
    "@Getter\n"
    "public class MqSettings {\n"
    "  private String queueName;\n"
    "}\n"
)
_PROPS_BOUND_SENDER = (
    "public class AlertQueueProducer extends AbstractEventProducer {\n"
    "  private JmsTemplate jmsTemplate;\n"
    "  private MqSettings settings;\n"
    "  void run() { jmsTemplate.convertAndSend(settings.getQueueName(), payload); }\n"
    "}\n"
)

_DECLARED_WRAPPER = (
    "public class NotifyEventService extends AbstractEventProducer {\n"
    "  private KafkaTemplate kafkaTemplate;\n"
    "  public void publishMessage(String topic, byte[] payload) {\n"
    "    kafkaTemplate.send(topic, payload);\n"
    "  }\n"
    "}\n"
)

_SECOND_ARG_SENDER = (
    "public class BroadcastProducer extends AbstractEventProducer {\n"
    "  private KafkaTemplate kafkaTemplate;\n"
    f'  private static final String ALERT_TOPIC = "{_MQ_TOPIC}";\n'
    "  void run() { kafkaTemplate.send(buildPayload(event), ALERT_TOPIC); }\n"
    "}\n"
)


class ProducerRealMirrorGapTests(unittest.TestCase):
    """Fixtures shaped after the RUNBOOK-42 real-mirror re-verify findings, not the earlier
    hand-written-getter synthetics those tests already covered."""

    def _scan(self, tmp, files):
        for rel, text in files.items():
            _write(os.path.join(tmp, "prod", rel), text)
        return pe.scan_repo("prod", os.path.join(tmp, "prod"))

    def _dest_for(self, records, symbol_substr):
        hits = [r for r in records if symbol_substr in r.get("destination_expression", "")]
        self.assertTrue(hits, f"no record whose expr contains {symbol_substr!r}")
        return hits[0]

    def test_lombok_getter_on_value_field_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/AlertConfig.java": _LOMBOK_VALUE_PRODUCER,
                "src/main/java/AlertProducer.java": _LOMBOK_SENDER,
                "src/main/resources/application.yml": f"alerts:\n  mq:\n    topic: {_MQ_TOPIC}\n",
            })
        rec = self._dest_for(records, "getQueueName")
        self.assertEqual(rec["destination"], _MQ_TOPIC)
        self.assertEqual(rec["routing_source"], "builder")
        self.assertEqual(rec["resolution_status"], "resolved")

    def test_lombok_getter_via_configuration_properties_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {
                "src/main/java/MqSettings.java": _PROPS_BOUND_CONFIG,
                "src/main/java/AlertQueueProducer.java": _PROPS_BOUND_SENDER,
                "src/main/resources/application.yml": f"alerts:\n  mq:\n    queue-name: {_MQ_TOPIC}\n",
            })
        rec = self._dest_for(records, "getQueueName")
        self.assertEqual(rec["destination"], _MQ_TOPIC)
        self.assertEqual(rec["resolution_status"], "resolved")

    def test_wrapper_method_declaration_is_not_a_call_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {"src/main/java/NotifyEventService.java": _DECLARED_WRAPPER})
        # exactly one send record (the real `kafkaTemplate.send(topic, payload)` call site) —
        # the `publishMessage(String topic, byte[] payload) {` declaration must not also count.
        sends = [r for r in records if r["routing_source"] != "wrapper"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["producer_type"], "kafkaTemplate.send")

    def test_destination_in_second_argument_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self._scan(tmp, {"src/main/java/BroadcastProducer.java": _SECOND_ARG_SENDER})
        const = _by(records, "routing_source", "constant")
        self.assertEqual([r["destination"] for r in const], [_MQ_TOPIC])


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

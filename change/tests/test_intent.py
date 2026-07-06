import unittest

from change.intent import ChangeRequest, parse_intent


class ParseIntentTests(unittest.TestCase):
    def test_rule_based_parser_extracts_endpoint_path_and_target_hint(self):
        request = parse_intent("please add a /status endpoint to the fixture service")

        self.assertEqual(request.kind, "add_endpoint")
        self.assertEqual(request.path, "/status")
        self.assertEqual(request.target_hint, "fixture service")
        self.assertIsNone(request.method)

    def test_rule_based_parser_rejects_garbled_asks(self):
        with self.assertRaises(ValueError):
            parse_intent("do something useful")

        with self.assertRaises(ValueError):
            parse_intent("add a /status endpoint")

    def test_injected_parser_uses_the_same_validated_contract(self):
        request = parse_intent(
            "ignored",
            parser=lambda text: {
                "kind": "add_endpoint",
                "target_hint": "fixture service",
                "path": "/health",
                "method": "healthCheck",
            },
        )

        self.assertEqual(
            request,
            ChangeRequest(
                kind="add_endpoint",
                target_hint="fixture service",
                path="/health",
                method="healthCheck",
            ),
        )

    def test_injected_parser_rejects_unsupported_kind(self):
        with self.assertRaises(ValueError):
            parse_intent(
                "ignored",
                parser=lambda text: {
                    "kind": "rewrite_everything",
                    "target_hint": "fixture service",
                    "path": "/status",
                },
            )


if __name__ == "__main__":
    unittest.main()

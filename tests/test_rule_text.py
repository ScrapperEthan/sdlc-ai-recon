"""Round B1/B2 tests for retriever/rule_text.py — structural AST + the operator-semantics seam.

Per docs/specs/use-case-uat-round-b-structural.md: the AST must NEVER assert operational meaning
(no initial_channels/fallback_edges) until interpret() is owner-confirmed.
"""
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, rule_text as rt


class ParseStructuralTests(unittest.TestCase):
    def test_single_channel_no_operator(self):
        ast = rt.parse("SMS")
        self.assertEqual(ast["mode"], "SINGLE")
        self.assertEqual(ast["channels"], ["SMS"])
        self.assertEqual(ast["operator_tree"], {"channel": "SMS"})
        self.assertEqual(ast["normalized_expression"], "SMS")
        self.assertEqual(ast["semantics"], "unconfirmed")
        self.assertEqual(ast["parse_warnings"], [])

    def test_fallback_chain(self):
        ast = rt.parse("EMAIL > SMS")
        self.assertEqual(ast["mode"], "FALLBACK")
        self.assertEqual(ast["channels"], ["EMAIL", "SMS"])
        self.assertEqual(ast["operator_tree"],
                          {"op": ">", "left": {"channel": "EMAIL"}, "right": {"channel": "SMS"}})
        self.assertEqual(ast["normalized_expression"], "EMAIL > SMS")

    def test_parallel(self):
        ast = rt.parse("SMS & EMAIL")
        self.assertEqual(ast["mode"], "PARALLEL")
        self.assertEqual(ast["operator_tree"],
                          {"op": "&", "left": {"channel": "SMS"}, "right": {"channel": "EMAIL"}})
        self.assertEqual(ast["normalized_expression"], "SMS & EMAIL")

    def test_parallel_then_fallback_with_parens(self):
        ast = rt.parse("(PUSH > SMS) & EMAIL")
        self.assertEqual(ast["mode"], "MIXED")
        self.assertEqual(set(ast["channels"]), {"PUSH", "SMS", "EMAIL"})
        self.assertEqual(ast["operator_tree"], {
            "op": "&",
            "left": {"op": ">", "left": {"channel": "PUSH"}, "right": {"channel": "SMS"}},
            "right": {"channel": "EMAIL"},
        })
        # explicit source parens are preserved on reprint even where precedence alone wouldn't
        # strictly require them, since they communicate the author's intended grouping
        self.assertEqual(ast["normalized_expression"], "(PUSH > SMS) & EMAIL")

    def test_upstream_selected(self):
        ast = rt.parse("PUSH | SMS | EMAIL")
        self.assertEqual(ast["mode"], "UPSTREAM_SELECTED")
        self.assertEqual(ast["channels"], ["EMAIL", "PUSH", "SMS"])
        self.assertEqual(ast["normalized_expression"], "PUSH | SMS | EMAIL")

    def test_letter_fallback_to_parallel_email_sms_canonical_case(self):
        # The I0141/I0142 canonical case from RUNBOOK-45 Part B: rule_text groups EMAIL & SMS as a
        # parallel fallback stage after LETTER — this is exactly the spec's worked example.
        ast = rt.parse("LETTER > (EMAIL & SMS)")
        self.assertEqual(ast["mode"], "MIXED")
        self.assertEqual(ast["operator_tree"], {
            "op": ">",
            "left": {"channel": "LETTER"},
            "right": {"op": "&", "left": {"channel": "EMAIL"}, "right": {"channel": "SMS"}},
        })
        self.assertEqual(ast["normalized_expression"], "LETTER > (EMAIL & SMS)")

    def test_duplicate_channel_flagged(self):
        ast = rt.parse("SMS > SMS")
        self.assertEqual(ast["channels"], ["SMS"])
        self.assertIn({"type": "duplicate_channel", "channel": "SMS"}, ast["parse_warnings"])

    def test_unknown_channel_token_flagged_not_a_crash(self):
        ast = rt.parse("SMS > CARRIER_PIGEON")
        types = {w["type"] for w in ast["parse_warnings"]}
        self.assertIn("unknown_channel", types)
        self.assertIn("CARRIER_PIGEON", ast["channels"])  # still surfaced, just flagged

    def test_blank_is_empty_mode(self):
        for value in ("", None, "   "):
            ast = rt.parse(value)
            self.assertEqual(ast["mode"], "EMPTY")
            self.assertEqual(ast["operator_tree"], None)
            self.assertEqual(ast["channels"], [])

    def test_unbalanced_parens_never_crashes(self):
        ast = rt.parse("LETTER > (EMAIL & SMS")
        types = {w["type"] for w in ast["parse_warnings"]}
        self.assertIn("unbalanced_parens", types)
        self.assertIsNone(ast["operator_tree"])
        # partial diagnostic info is still useful even though the tree is unreliable
        self.assertEqual(set(ast["channels"]), {"LETTER", "EMAIL", "SMS"})

    def test_extra_closing_paren_never_crashes(self):
        ast = rt.parse("SMS > EMAIL)")
        types = {w["type"] for w in ast["parse_warnings"]}
        self.assertIn("unbalanced_parens", types)

    def test_literal_backslash_flagged_same_bug_class_as_runtime(self):
        # The runtime's `ruleText.contains("\\|")` matches a literal backslash, not the regex `|` —
        # this catches the same class of artifact if it ever appears in raw rule_text.
        ast = rt.parse("SMS \\| EMAIL")
        types = {w["type"] for w in ast["parse_warnings"]}
        self.assertIn("literal_escape_artifact", types)

    def test_left_associative_same_operator_chain_no_extra_parens(self):
        ast = rt.parse("LETTER > EMAIL > SMS")
        self.assertEqual(ast["normalized_expression"], "LETTER > EMAIL > SMS")
        self.assertEqual(ast["operator_tree"], {
            "op": ">",
            "left": {"op": ">", "left": {"channel": "LETTER"}, "right": {"channel": "EMAIL"}},
            "right": {"channel": "SMS"},
        })


class InterpretUnconfirmedByDefaultTests(unittest.TestCase):
    def test_default_semantics_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "absent.json")
            with mock.patch.object(config, "RULE_TEXT_SEMANTICS_JSON", missing):
                ast = rt.parse("LETTER > (EMAIL & SMS)")
                result = rt.interpret(ast)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "operator semantics not owner-confirmed")
        self.assertEqual(set(result["unconfirmed_operators"]), {">", "&"})

    def test_single_channel_no_operator_is_always_available(self):
        # No operator used at all -> nothing to confirm.
        ast = rt.parse("SMS")
        result = rt.interpret(ast, semantics=rt.DEFAULT_SEMANTICS)
        self.assertTrue(result["available"])
        self.assertEqual(result["initial_channels"], ["SMS"])

    def test_empty_ast_is_unavailable_not_a_crash(self):
        ast = rt.parse("")
        result = rt.interpret(ast)
        self.assertFalse(result["available"])


class InterpretConfirmedSeamTests(unittest.TestCase):
    """Once an owner fills in index/rule_text_semantics.json, interpretation lights up with zero
    code change here — this is the seam itself, exercised with a fixture 'confirmed' file."""

    _CONFIRMED = {
        ">": {"meaning": "sequential_fallback"},
        "&": {"meaning": "parallel_send"},
        "|": {"meaning": "upstream_selected"},
    }

    def test_fallback_confirmed_yields_edges(self):
        ast = rt.parse("EMAIL > SMS")
        result = rt.interpret(ast, semantics=self._CONFIRMED)
        self.assertTrue(result["available"])
        self.assertEqual(result["initial_channels"], ["EMAIL"])
        self.assertEqual(result["fallback_edges"], [["EMAIL", "SMS"]])

    def test_parallel_confirmed_yields_group(self):
        ast = rt.parse("SMS & EMAIL")
        result = rt.interpret(ast, semantics=self._CONFIRMED)
        self.assertTrue(result["available"])
        self.assertEqual(result["initial_channels"], ["SMS", "EMAIL"])
        self.assertEqual(result["parallel_groups"], [["SMS", "EMAIL"]])

    def test_mixed_confirmed_letter_fallback_to_parallel(self):
        ast = rt.parse("LETTER > (EMAIL & SMS)")
        result = rt.interpret(ast, semantics=self._CONFIRMED)
        self.assertTrue(result["available"])
        self.assertEqual(result["initial_channels"], ["LETTER"])
        self.assertEqual(sorted(result["fallback_edges"]),
                          sorted([["LETTER", "EMAIL"], ["LETTER", "SMS"]]))

    def test_upstream_selected_confirmed_yields_selectable(self):
        ast = rt.parse("PUSH | SMS | EMAIL")
        result = rt.interpret(ast, semantics=self._CONFIRMED)
        self.assertTrue(result["available"])
        self.assertEqual(result["selectable_channels"], ["EMAIL", "PUSH", "SMS"])

    def test_partially_confirmed_still_unavailable(self):
        # Only '>' confirmed, but this expression also uses '&' -> still unconfirmed overall.
        ast = rt.parse("LETTER > (EMAIL & SMS)")
        result = rt.interpret(ast, semantics={">": {"meaning": "sequential_fallback"}})
        self.assertFalse(result["available"])
        self.assertEqual(result["unconfirmed_operators"], ["&"])


if __name__ == "__main__":
    unittest.main()

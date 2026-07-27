"""The 2026-07-27 owner answer on rule_text semantics, and the fail-closed guards around it.

Owner's words: `>` 哪个大就哪个先发 / `&` 一起发 / `|` 二选一 / 以 rule_text 为准,没有的情况下再看
priority. These tests pin that reading AND pin the two behaviours that must survive it: an
unconfirmed config still refuses to guess, and a config declaring a meaning we do not implement
fails CLOSED rather than being interpreted with today's structural assumptions.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import rule_text as rt
from retriever import usecase_consistency as ucc

CONFIRMED = {
    ">": {"meaning": "ordered_precedence"},
    "&": {"meaning": "parallel_all"},
    "|": {"meaning": "exclusive_choice"},
    "precedence": ["|", ">", "&"],
    "stage_transition": "unconfirmed",
    "source_precedence": {"order": ["rule_text", "channel_rule_priority"]},
}

# The canonical disagreement from RUNBOOK-45 Part B.
I0141 = "LETTER > (EMAIL & SMS)"


class ShippedConfigTest(unittest.TestCase):
    """The committed config/rule_text_semantics.json must actually carry the owner's answer —
    a typo there would silently switch interpretation back off for the whole estate."""

    def test_shipped_config_confirms_all_three_operators(self):
        semantics = rt.load_semantics()
        for op, expected in ((">", "ordered_precedence"), ("&", "parallel_all"),
                             ("|", "exclusive_choice")):
            self.assertEqual(semantics[op]["meaning"], expected, f"operator {op}")
            self.assertTrue(rt._op_confirmed(semantics, op), f"operator {op} should be confirmed")

    def test_shipped_config_makes_rule_text_authoritative(self):
        self.assertEqual(rt.source_precedence(), ["rule_text", "channel_rule_priority"])

    def test_stage_transition_stays_unconfirmed(self):
        # The owner confirmed left-sends-first, NOT whether the next stage fires only on failure.
        self.assertEqual(rt.load_semantics().get("stage_transition"), "unconfirmed")


class InterpretationTest(unittest.TestCase):
    def test_i0141_interprets_as_letter_first_then_email_and_sms_together(self):
        result = rt.interpret(rt.parse(I0141), CONFIRMED)
        self.assertTrue(result["available"])
        self.assertEqual(result["initial_channels"], ["LETTER"])          # 哪个大就哪个先发
        self.assertEqual(result["parallel_groups"], [["EMAIL", "SMS"]])   # & = 一起发
        self.assertEqual(sorted(result["fallback_edges"]),
                         [["LETTER", "EMAIL"], ["LETTER", "SMS"]])
        self.assertEqual(result["stage_transition"], "unconfirmed")

    def test_pipe_is_exclusive_choice_not_a_send_order(self):
        result = rt.interpret(rt.parse("EMAIL | SMS"), CONFIRMED)
        self.assertTrue(result["available"])
        self.assertEqual(result["selectable_channels"], ["EMAIL", "SMS"])  # 二选一
        self.assertEqual(result["initial_channels"], [])
        self.assertEqual(result["fallback_edges"], [])

    def test_single_channel_needs_no_operator_confirmation(self):
        result = rt.interpret(rt.parse("SMS"), rt.DEFAULT_SEMANTICS)
        self.assertTrue(result["available"])
        self.assertEqual(result["initial_channels"], ["SMS"])


class FailsClosedTest(unittest.TestCase):
    def test_unconfirmed_config_still_refuses_to_guess(self):
        result = rt.interpret(rt.parse(I0141), rt.DEFAULT_SEMANTICS)
        self.assertFalse(result["available"])
        self.assertEqual(result["unconfirmed_operators"], ["&", ">"])

    def test_unimplemented_meaning_fails_closed_rather_than_misinterpreting(self):
        # If '>' is ever REDEFINED to something this module does not implement, we must switch
        # interpretation off — not keep emitting today's ordered-stage shape under a new name.
        redefined = dict(CONFIRMED, **{">": {"meaning": "broadcast_to_all"}})
        result = rt.interpret(rt.parse(I0141), redefined)
        self.assertFalse(result["available"])
        self.assertEqual(result["unconfirmed_operators"], [">"])

    def test_missing_config_file_yields_the_safe_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.json")
            with mock.patch.object(rt.config, "RULE_TEXT_SEMANTICS_JSON", missing):
                self.assertIsNone(rt.source_precedence())
                self.assertFalse(rt._op_confirmed(rt.load_semantics(), ">"))

    def test_readme_only_config_does_not_confirm_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "semantics.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"_README": ["notes only"]}, handle)
            with mock.patch.object(rt.config, "RULE_TEXT_SEMANTICS_JSON", path):
                self.assertFalse(rt._op_confirmed(rt.load_semantics(), "&"))


class ConsistencyResolutionTest(unittest.TestCase):
    """The validator must keep REPORTING the disagreement (it is a real data problem) while
    naming the owner-confirmed winner and demoting the severity."""

    RULES = [
        {"channel": "LETTER", "priority": "1", "citation": "channel_rule.csv:2"},
        {"channel": "EMAIL", "priority": "2", "citation": "channel_rule.csv:3"},
        {"channel": "SMS", "priority": "3", "citation": "channel_rule.csv:4"},
    ]
    EXT = {"rule_text": I0141, "citation": "ext.csv:9"}

    def _check(self, semantics_return):
        with mock.patch.object(ucc.rt, "source_precedence", return_value=semantics_return):
            return ucc.check_use_case(
                "I0141", rules_idx={"i0141": self.RULES}, ext_idx={"i0141": self.EXT})

    def test_confirmed_owner_answer_resolves_the_disagreement(self):
        findings = self._check(["rule_text", "channel_rule_priority"])
        clash = [f for f in findings if f["check"] == "expression_vs_priority"]
        self.assertEqual(len(clash), 1)
        self.assertEqual(clash[0]["severity"], "warning")  # resolvable, no longer an open question
        self.assertIn("rule_text is authoritative", clash[0]["resolution"])
        # Still cites BOTH sources — the point of the finding is that two tables disagree.
        self.assertIn("ext.csv:9", clash[0]["citations"])
        self.assertIn("channel_rule.csv:3", clash[0]["citations"])

    def test_unconfirmed_preserves_the_original_no_winner_behaviour(self):
        findings = self._check(None)
        clash = [f for f in findings if f["check"] == "expression_vs_priority"]
        self.assertEqual(len(clash), 1)
        self.assertEqual(clash[0]["severity"], "error")
        self.assertNotIn("resolution", clash[0])

    def test_blank_rule_text_is_the_documented_fallback_not_a_warning(self):
        ext = {"rule_text": "", "citation": "ext.csv:9"}
        with mock.patch.object(ucc.rt, "source_precedence",
                               return_value=["rule_text", "channel_rule_priority"]):
            findings = ucc.check_use_case(
                "I0141", rules_idx={"i0141": self.RULES}, ext_idx={"i0141": ext})
        blank = [f for f in findings if f["check"] == "blank_with_rules"]
        self.assertEqual(len(blank), 1)
        self.assertEqual(blank[0]["severity"], "info")  # 没有的情况下再看 priority = normal
        self.assertIn("priority applies", blank[0]["resolution"])

    def test_blank_rule_text_stays_a_warning_while_unconfirmed(self):
        ext = {"rule_text": "", "citation": "ext.csv:9"}
        with mock.patch.object(ucc.rt, "source_precedence", return_value=None):
            findings = ucc.check_use_case(
                "I0141", rules_idx={"i0141": self.RULES}, ext_idx={"i0141": ext})
        blank = [f for f in findings if f["check"] == "blank_with_rules"]
        self.assertEqual(blank[0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()

"""The 2026-08-05 owner answers on traffic_percentage and the tbl_use_case code tables.

Two answers, deliberately implemented with different strengths:

* **`traffic_percentage = 0` means the channel does not send.** Definite, so the engine acts on it.
* **A blank `vendor` is "基本上" because the percentage is 0.** Explicitly *mostly*, so the engine
  CHECKS it per row and reports where it fails. Encoding "mostly" as a rule would have hidden
  exactly the rows worth looking at — a live route with no recorded carrier.

Plus the business_category provenance split: the data dictionary defines 0-7, BusinessCategoryEnum
.java additionally defines 8/10-21/32/34/35, and the real UAT rows additionally CONTAIN 33 and 37.
Only the last group is a defect, and flattening the three sources into one "known enum" is what
previously produced the wrong claim that 33/37 were unregistered new categories.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import rule_text as rt
from retriever import traffic, usecase_catalog, usecase_consistency as ucc, usecase_router


class ReadPercentageTest(unittest.TestCase):
    def test_zero_does_not_send(self):
        reading = traffic.read("0")
        self.assertIs(reading["sends"], False)
        self.assertTrue(reading["known"])

    def test_positive_sends(self):
        self.assertIs(traffic.read("100")["sends"], True)
        self.assertIs(traffic.read("0.5")["sends"], True)
        self.assertIs(traffic.read("50%")["sends"], True)

    def test_blank_is_unknown_NOT_zero(self):
        # The dangerous default. Reading blank as 0 would silently retire live channels — worse
        # than a fake decoding, because it removes something real instead of adding something false.
        for value in ("", "   ", None):
            reading = traffic.read(value)
            self.assertIsNone(reading["sends"], repr(value))
            self.assertFalse(reading["known"])
            self.assertIn("NOT zero", reading["note"])

    def test_unparseable_is_unknown_not_zero(self):
        reading = traffic.read("n/a")
        self.assertIsNone(reading["sends"])
        self.assertIn("not a number", reading["note"])

    def test_out_of_range_is_reported_not_clamped(self):
        reading = traffic.read("150")
        self.assertEqual(reading["value"], 150.0)
        self.assertFalse(reading["in_range"])
        self.assertIs(reading["sends"], True)   # whatever it means, it is not zero
        self.assertIn("not clamped", reading["note"])


class VerdictTest(unittest.TestCase):
    def test_any_sending_row_makes_the_channel_live(self):
        rows = [{"traffic_percentage": "0"}, {"traffic_percentage": "100"}]
        self.assertIs(traffic.verdict(rows, "SMS")["sends"], True)

    def test_all_zero_makes_the_channel_idle(self):
        rows = [{"traffic_percentage": "0"}, {"traffic_percentage": "0"}]
        entry = traffic.verdict(rows, "SMS")
        self.assertIs(entry["sends"], False)
        self.assertEqual(entry["idle"], 2)

    def test_zero_plus_unreadable_is_unknown_not_idle(self):
        # Retiring a channel needs positive evidence about EVERY row.
        rows = [{"traffic_percentage": "0"}, {"traffic_percentage": ""}]
        self.assertIsNone(traffic.verdict(rows, "SMS")["sends"])

    def test_no_rows_at_all_is_unknown(self):
        self.assertIsNone(traffic.verdict([], "SMS")["sends"])

    def test_summarise_groups_by_channel(self):
        rows = [{"channel": "SMS", "traffic_percentage": "0"},
                {"channel": "EMAIL", "traffic_percentage": "100"}]
        by_channel = {entry["channel"]: entry["sends"] for entry in traffic.summarise(rows)}
        self.assertEqual(by_channel, {"SMS": False, "EMAIL": True})
        self.assertEqual(traffic.idle_channels(rows), ["SMS"])

    def test_summarise_honours_a_caller_supplied_normaliser(self):
        # PUSH+INBOX / PUSH_INBOX are one channel; without the normaliser they come back as two
        # half-answers, and a channel that is half-idle reads as neither.
        rows = [{"channel": "PUSH+INBOX", "traffic_percentage": "0"},
                {"channel": "PUSH_INBOX", "traffic_percentage": "0"}]
        merged = traffic.summarise(rows, key=lambda name: (name or "").replace("+", "_").upper())
        self.assertEqual(len(merged), 1)
        self.assertIs(merged[0]["sends"], False)
        self.assertEqual(merged[0]["rules"], 2)


class BlankVendorExplanationTest(unittest.TestCase):
    """`vendor` blank + `traffic_percentage` 0 = explained. Blank + real traffic = the residue."""

    def test_zero_traffic_explains_the_blank(self):
        result = usecase_router._explain_blank_vendor(traffic.read("0"))
        self.assertIs(result["holds"], True)
        self.assertIn("Nothing is missing here", result["note"])

    def test_live_traffic_does_not_explain_the_blank(self):
        result = usecase_router._explain_blank_vendor(traffic.read("100"))
        self.assertIs(result["holds"], False)
        self.assertIn("IS carrying traffic", result["note"])
        self.assertIn("do not fill it in", result["note"])

    def test_unknown_traffic_leaves_the_explanation_untested(self):
        result = usecase_router._explain_blank_vendor(traffic.read(""))
        self.assertIsNone(result["holds"])

    def test_present_vendor_gets_no_explanation_field(self):
        index = {"available": True, "table_present": True, "row_count": 1,
                 "key_fields": ["business_category", "channel", "route", "router"],
                 "unbound_key_fields": [],
                 "index": {("1", "sms", "r1", "rt1"): [
                     {"id": "9", "vendor": "HTCL", "delivery_path": "",
                      "citation": "router.csv:2"}]}}
        rule = {"channel": "SMS", "route": "R1", "router": "RT1", "business_category": "1",
                "traffic_percentage": "0"}
        result = usecase_router.router_for_rule(rule, index=index)
        self.assertTrue(result["matched"])
        self.assertNotIn("vendor_blank_explained", result)

    def test_blank_vendor_on_a_matched_row_carries_the_verdict(self):
        index = {"available": True, "table_present": True, "row_count": 1,
                 "key_fields": ["business_category", "channel", "route", "router"],
                 "unbound_key_fields": [],
                 "index": {("1", "email", "r1", "rt1"): [
                     {"id": "9", "vendor": "", "delivery_path": "", "citation": "router.csv:2"}]}}
        rule = {"channel": "EMAIL", "route": "R1", "router": "RT1", "business_category": "1",
                "traffic_percentage": "80"}
        result = usecase_router.router_for_rule(rule, index=index)
        self.assertTrue(result["matched"])
        self.assertIs(result["vendor_blank_explained"]["holds"], False)
        self.assertIs(result["traffic"]["sends"], True)


class BusinessCategoryProvenanceTest(unittest.TestCase):
    def test_dictionary_codes_are_marked_authoritative(self):
        resolved = usecase_catalog.resolve_business_category("6")
        self.assertEqual(resolved["label"], "CMB")
        self.assertEqual(resolved["source"], "data_dictionary")
        self.assertTrue(resolved["known"])

    def test_code_only_codes_are_known_but_not_dictionary_backed(self):
        # 32 IS defined — in BusinessCategoryEnum.java, not in the dictionary excerpt. Reporting it
        # as drift would be wrong; reporting it as dictionary-backed would also be wrong.
        resolved = usecase_catalog.resolve_business_category("32")
        self.assertEqual(resolved["source"], "code_enum")
        self.assertTrue(resolved["known"])

    def test_the_real_uat_outliers_are_defined_nowhere(self):
        for code in ("33", "37"):
            resolved = usecase_catalog.resolve_business_category(code)
            self.assertEqual(resolved["source"], "undefined", code)
            self.assertFalse(resolved["known"], code)

    def test_blank_and_garbage_are_distinguishable(self):
        self.assertEqual(usecase_catalog.resolve_business_category("")["source"], "absent")
        self.assertEqual(usecase_catalog.resolve_business_category("abc")["source"], "unparseable")

    def test_legacy_label_helper_is_unchanged(self):
        # Every existing caller reads labels through this; the provenance is additive.
        self.assertEqual(usecase_catalog._category_label("6"), "CMB")
        self.assertEqual(usecase_catalog._category_label("33"), "UNKNOWN(33)")
        self.assertEqual(usecase_catalog._category_label(""), "UNKNOWN()")

    def test_config_can_promote_a_code_without_a_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "business_enums.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"business_category": {
                    "data_dictionary": {"33": "NEWLY_REGISTERED"},
                    "code_enum": {"32": "HSBC_WPB_SERVICING_BATCH"},
                }}, handle)
            with mock.patch.object(usecase_catalog.config, "BUSINESS_ENUMS_JSON", path):
                resolved = usecase_catalog.resolve_business_category("33")
                self.assertEqual(resolved["source"], "data_dictionary")
                self.assertEqual(resolved["label"], "NEWLY_REGISTERED")

    def test_a_code_in_both_halves_counts_as_dictionary_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "business_enums.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"business_category": {
                    "data_dictionary": {"5": "FROM_DICT"},
                    "code_enum": {"5": "FROM_CODE"},
                }}, handle)
            with mock.patch.object(usecase_catalog.config, "BUSINESS_ENUMS_JSON", path):
                dictionary, code_only = usecase_catalog.business_category_sources()
                self.assertIn(5, dictionary)
                self.assertNotIn(5, code_only)

    def test_missing_config_falls_back_to_the_built_in_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(usecase_catalog.config, "BUSINESS_ENUMS_JSON",
                                    os.path.join(tmp, "absent.json")):
                self.assertEqual(usecase_catalog.resolve_business_category("6")["label"], "CMB")
                self.assertEqual(usecase_catalog.resolve_business_category("32")["source"],
                                 "code_enum")

    def test_shipped_config_matches_the_built_in_seed(self):
        # The committed config and the in-code seed must agree, or the fallback silently changes
        # answers depending on whether the file is present.
        dictionary, code_only = usecase_catalog.business_category_sources()
        self.assertEqual(dictionary, usecase_catalog.BUSINESS_CATEGORY_DICTIONARY)
        self.assertEqual(code_only, usecase_catalog.BUSINESS_CATEGORY_CODE_ONLY)


class SendModeTest(unittest.TestCase):
    def test_dictionary_codes_resolve(self):
        self.assertEqual(usecase_catalog.resolve_send_mode("1")["label"], "Send at the same time")
        self.assertEqual(usecase_catalog.resolve_send_mode("2")["label"], "Send by priority")
        self.assertEqual(usecase_catalog.resolve_send_mode("3")["label"], "Send by single channel")

    def test_each_mode_maps_onto_a_rule_text_meaning(self):
        # send_mode is an INDEPENDENT third statement of the same semantics; the mapping is what
        # makes a cross-check possible. It never overrides rule_text.
        self.assertEqual(usecase_catalog.resolve_send_mode("1")["rule_text_equivalent"],
                         "parallel_all")
        self.assertEqual(usecase_catalog.resolve_send_mode("2")["rule_text_equivalent"],
                         "ordered_precedence")
        self.assertEqual(usecase_catalog.resolve_send_mode("3")["rule_text_equivalent"],
                         "exclusive_choice")

    def test_absent_column_is_a_clean_blank_not_a_guess(self):
        resolved = usecase_catalog.resolve_send_mode("")
        self.assertEqual(resolved["label"], "")
        self.assertFalse(resolved["known"])

    def test_unknown_code_is_not_invented(self):
        resolved = usecase_catalog.resolve_send_mode("9")
        self.assertEqual(resolved["code"], 9)
        self.assertEqual(resolved["label"], "")
        self.assertFalse(resolved["known"])

    def test_send_mode_binds_exact_only_so_it_cannot_take_send_policy(self):
        # A fuzzy ("send",) fallback would let send_policy bind here — the first-column-wins defect
        # that made `status` bind to `unknown_bounce_back_status`.
        spec = usecase_catalog._FIELD_SPECS["send_mode"]
        self.assertEqual(spec["needles"], ())
        self.assertIn("sendmode", spec["exact"])


if __name__ == "__main__":
    unittest.main()


class SendModeCrossCheckTest(unittest.TestCase):
    """send_mode is a THIRD registration of the send semantics. It cross-checks rule_text; it never
    overrides it (rule_text is authoritative, owner-confirmed 2026-07-27)."""

    def _check(self, rule_text, send_mode):
        ast = rt.parse(rule_text)
        identity = {"use_case_id": "X0001", "send_mode_code": send_mode}
        return ucc._send_mode_finding("X0001", ast, identity, ["ext.csv:9"])

    def test_agreement_produces_no_finding(self):
        self.assertIsNone(self._check("EMAIL & SMS", "1"))    # same time  <-> &
        self.assertIsNone(self._check("LETTER > EMAIL", "2"))  # by priority <-> >
        self.assertIsNone(self._check("EMAIL | SMS", "3"))     # single      <-> |

    def test_disagreement_is_reported_with_rule_text_named_as_the_winner(self):
        finding = self._check("EMAIL & SMS", "2")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["check"], "send_mode_vs_rule_text")
        self.assertEqual(finding["severity"], "warning")
        self.assertIn("Send by priority", finding["message"])
        self.assertIn("PARALLEL", finding["message"])
        self.assertIn("rule_text is authoritative", finding["resolution"])

    def test_mixed_expressions_cannot_contradict_a_single_code(self):
        # "(SMS > EMAIL) & PUSH" genuinely combines operators — no single send_mode is wrong for it.
        self.assertIsNone(self._check("(SMS > EMAIL) & PUSH", "1"))
        self.assertIsNone(self._check("(SMS > EMAIL) & PUSH", "2"))

    def test_absent_column_costs_nothing(self):
        # We have only seen send_mode in a data dictionary, never in an export header.
        self.assertIsNone(self._check("EMAIL & SMS", ""))
        self.assertIsNone(self._check("EMAIL & SMS", None))

    def test_unrecognised_code_is_not_treated_as_a_disagreement(self):
        self.assertIsNone(self._check("EMAIL & SMS", "9"))

    def test_no_identity_at_all_costs_nothing(self):
        ast = rt.parse("EMAIL & SMS")
        self.assertIsNone(ucc._send_mode_finding("X0001", ast, None, []))

    def test_unparseable_rule_text_is_not_compared(self):
        self.assertIsNone(self._check("EMAIL &&& SMS", "1"))


class RangeTextTest(unittest.TestCase):
    """The finding names what IS defined, so it has to print twenty codes compactly."""

    def test_contiguous_codes_collapse_to_a_span(self):
        self.assertEqual(ucc._range_text({0: "", 1: "", 2: "", 3: ""}), "0-3")

    def test_gaps_are_preserved(self):
        self.assertEqual(ucc._range_text({8: "", 10: "", 11: "", 32: ""}), "8, 10-11, 32")

    def test_single_code_is_not_printed_as_a_span(self):
        self.assertEqual(ucc._range_text({6: ""}), "6")

    def test_empty_is_named_not_blank(self):
        self.assertEqual(ucc._range_text({}), "none")

    def test_the_shipped_dictionary_range_reads_as_the_photo_shows(self):
        dictionary, _code_only = usecase_catalog.business_category_sources()
        self.assertEqual(ucc._range_text(dictionary), "0-7")

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

from retriever import config, delivery_chain
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


class StandbyNotOffTest(unittest.TestCase):
    """0% is a provisioned SECOND CARRIER, not a switched-off route.

    The messaging team's routing rules create 0% rows deliberately:

        if message is high-risk, choose dual vendor with HTCL and CSL,
        primary 100% HTCL & 0% for CSL

        if need to send to CN, choose LX (not yet ready) and CM routers,
        primary 100% CM & 0% for LX

    The first version of this module read 0% as "does not send, do not count as live". That answers
    "the HTCL vendor is down, what takes over?" by deleting CSL — the only correct answer. These
    tests pin the corrected reading, because the failure only shows up during a real outage, which
    is the worst possible time to discover it.
    """

    def test_zero_is_flagged_as_standby_not_merely_absent(self):
        reading = traffic.read("0")
        self.assertIs(reading["sends"], False)
        self.assertIs(reading["standby"], True)

    def test_the_note_says_an_outage_answer_must_include_it(self):
        note = traffic.read("0")["note"]
        self.assertIn("takes over", note)
        self.assertIn("MUST include it", note)
        # And it must not claim to know WHICH of the three causes applies.
        self.assertIn("cannot tell the three apart", note)

    def test_unknown_percentage_is_not_standby_either(self):
        # standby is a positive claim about a provisioned route; a blank supports no claim at all.
        for value in ("", "n/a", None):
            self.assertIsNone(traffic.read(value)["standby"], repr(value))

    def test_a_live_channel_with_a_zero_pct_row_still_reports_its_standby(self):
        # The dual-vendor case: 100% HTCL + 0% CSL is ONE channel that IS sending. A channel-level
        # verdict alone hides the standby completely.
        rows = [{"traffic_percentage": "100"}, {"traffic_percentage": "0"}]
        entry = traffic.verdict(rows, "SMS")
        self.assertIs(entry["sends"], True)
        self.assertTrue(entry["has_standby"])
        self.assertEqual(entry["standby_rules"], 1)

    def test_standby_channels_is_the_honest_name_and_idle_still_works(self):
        rows = [{"channel": "SMS", "traffic_percentage": "0"}]
        self.assertEqual(traffic.standby_channels(rows), ["SMS"])
        self.assertEqual(traffic.idle_channels(rows), ["SMS"])   # old name, same rows

    def test_exit_path_caveat_tells_the_reader_to_include_them_in_an_outage(self):
        by_channel = [{"channel": "sms",
                       "traffic": {"sends": False, "has_standby": True},
                       "vendor_selection": {"method": "channel_upper_bound"},
                       "vendors": [], "vendors_off_diagram": [], "terminals": []}]
        standby = [c["channel"] for c in by_channel if c["traffic"]["sends"] is False]
        self.assertEqual(standby, ["sms"])   # the population the caveat is built from

    def test_report_tells_the_reader_to_include_standby_in_a_blast_radius(self):
        import impact_report
        lines = impact_report.render_delivery_chain({
            "available": True, "channel_source": "declared", "by_channel": [],
            "traffic": {"standby_channels": ["sms"], "channels_with_a_standby_route": ["email"],
                        "sending_channels": [], "unknown_channels": []},
        })
        text = chr(10).join(lines)
        self.assertIn("standby", text.lower())
        self.assertIn("include", text.lower())
        self.assertIn("dual-vendor", text.lower())
        # The old wording actively told the reader to exclude them. It must be gone.
        self.assertNotIn("do not count these as live", text.lower())


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

    def test_shipped_config_matches_the_built_in_send_mode_seed(self):
        # The config WINS when present, so a code added only to the in-code seed silently does
        # nothing. That is exactly what happened while wiring codes 4/5 — caught by a test, which
        # is the point. Any drift between the two must fail here.
        self.assertEqual(usecase_catalog.send_mode_enum(), usecase_catalog.SEND_MODE_ENUM)
        self.assertEqual(usecase_catalog.send_mode_rule_text_equivalent(),
                         usecase_catalog.SEND_MODE_RULE_TEXT_EQUIVALENT)
        self.assertEqual(sorted(usecase_catalog.send_mode_pending()),
                         sorted(usecase_catalog.SEND_MODE_PENDING_MEANING))

    def test_code_zero_is_pending_not_undefined(self):
        # Two different statements. "Undefined" means drift; "pending" means a legitimate value we
        # cannot read yet. 903 rows makes the second one the honest description.
        resolved = usecase_catalog.resolve_send_mode("0")
        self.assertFalse(resolved["known"])
        self.assertTrue(resolved["pending"])
        self.assertIn("903", resolved["note"])

    def test_a_genuinely_unexpected_code_is_neither_known_nor_pending(self):
        resolved = usecase_catalog.resolve_send_mode("99")
        self.assertFalse(resolved["known"])
        self.assertFalse(resolved["pending"])

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

    def test_mixed_mode_became_comparable_when_code_5_arrived(self):
        # Before 2026-08-06 a MIXED expression could not be checked at all: no code named that
        # shape, so it could neither agree nor disagree. The full dictionary gave 5 = "Mixed mode",
        # which is literally what rule_text calls MIXED — so it now checks positively.
        self.assertIsNone(self._check("(SMS > EMAIL) & PUSH", "5"))
        finding = self._check("(SMS > EMAIL) & PUSH", "1")
        self.assertIsNotNone(finding)
        self.assertIn("MIXED", finding["message"])

    def test_code_4_has_no_rule_text_equivalent_so_nothing_is_compared(self):
        # "Send by separately" has no operator in the expression grammar. Mapping it to the
        # nearest-looking one would manufacture agreement or disagreement out of nothing.
        self.assertEqual(usecase_catalog.resolve_send_mode("4")["label"], "Send by separately")
        self.assertEqual(usecase_catalog.resolve_send_mode("4")["rule_text_equivalent"], "")
        for expression in ("EMAIL & SMS", "LETTER > EMAIL", "EMAIL | SMS"):
            self.assertIsNone(self._check(expression, "4"), expression)

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


class ThreeTableJoinTest(unittest.TestCase):
    """The four-column router key needs a business_category, and it usually lives on the MASTER
    table — so reaching a carrier is a THREE-table join, not two.

    RUNBOOK-75's first C1 snippet forgot to supply it and reported 0 matches on a dataset that
    actually matches thousands of rows, which reads as "this table does not join" rather than "my
    script passed an empty key". The engine was correct throughout; only the verification script
    was wrong. These tests pin the distinction so a future snippet cannot make the same mistake
    silently.

    Deliberately synthetic: no snapshot counts are asserted here. Real numbers belong in a runbook
    report, not in a test that would go red the next time the export changes.
    """

    KEY = ["business_category", "channel", "route", "router"]

    def _index(self, category="1"):
        return {"available": True, "table_present": True, "row_count": 1,
                "key_fields": list(self.KEY), "unbound_key_fields": [],
                "index": {(category, "sms", "r1", "rt1"): [
                    {"id": "9", "vendor": "HTCL", "delivery_path": "",
                     "citation": "router.csv:2"}]}}

    def _rule(self, **over):
        rule = {"channel": "SMS", "route": "R1", "router": "RT1", "traffic_percentage": "100"}
        rule.update(over)
        return rule

    def test_rule_without_its_own_category_needs_the_master_one(self):
        # The exact failure mode: no category anywhere -> incomplete key -> no match, with a reason.
        missed = usecase_router.router_for_rule(self._rule(), "", index=self._index())
        self.assertFalse(missed["matched"])
        self.assertIn("incomplete natural key", missed["reason"])
        self.assertIn("business_category", missed["missing_key_fields"])

        # Supply the master category and the SAME rule matches.
        hit = usecase_router.router_for_rule(self._rule(), "1", index=self._index())
        self.assertTrue(hit["matched"])
        self.assertEqual(hit["business_category_source"], "master")

    def test_a_rule_carrying_its_own_category_does_not_need_the_master(self):
        hit = usecase_router.router_for_rule(
            self._rule(business_category="1"), "", index=self._index())
        self.assertTrue(hit["matched"])
        self.assertEqual(hit["business_category_source"], "rule")

    def test_the_rows_own_category_wins_and_the_disagreement_is_reported(self):
        # Row-local wins, but silently preferring it would hide that the two tables point at
        # different carriers' rows — which is the failure RUNBOOK-54 existed to prevent.
        hit = usecase_router.router_for_rule(
            self._rule(business_category="1"), "2", index=self._index())
        self.assertTrue(hit["matched"])
        self.assertIn("business_category_conflict", hit)

    def test_an_empty_master_category_never_matches_by_dropping_the_column(self):
        # The dangerous "fix" would be to match on the remaining three columns when the category is
        # blank. That lands a use case on an unrelated carrier's row.
        result = usecase_router.router_for_rule(self._rule(), "", index=self._index(category=""))
        self.assertFalse(result["matched"])


class LocalEnumOverrideTest(unittest.TestCase):
    """The intranet cannot push, so an owner answer has to be writable on the box that day.

    The real export carries send_mode codes 0/4/5 that nobody has defined yet. When the owner
    explains them, editing the TRACKED config on the box would leave an uncommitted change to a
    tracked file and refuse their next `git pull` — one config blocking every unrelated fix in the
    same pull. `config/business_enums.local.json` is gitignored and auto-preferred, so it costs
    them no round trip through this repo.
    """

    def test_local_file_is_preferred_over_the_committed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "config"))
            for name in ("business_enums.json", "business_enums.local.json"):
                with open(os.path.join(tmp, "config", name), "w", encoding="utf-8") as handle:
                    json.dump({"_which": name}, handle)
            with mock.patch.object(config, "ROOT", tmp):
                self.assertTrue(
                    config._cfg_local("SDLC_NOPE", "business_enums.json").endswith(".local.json"))

    def test_committed_file_is_used_when_no_local_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "config"))
            with mock.patch.object(config, "ROOT", tmp):
                path = config._cfg_local("SDLC_NOPE", "business_enums.json")
            self.assertTrue(path.endswith("business_enums.json"))
            self.assertNotIn(".local.", path)

    def test_env_var_still_wins_over_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "config"))
            with open(os.path.join(tmp, "config", "business_enums.local.json"), "w") as handle:
                handle.write("{}")
            with mock.patch.object(config, "ROOT", tmp),                  mock.patch.dict(os.environ, {"SDLC_BE_TEST": "/elsewhere/x.json"}):
                self.assertEqual(config._cfg_local("SDLC_BE_TEST", "business_enums.json"),
                                 "/elsewhere/x.json")

    def test_a_local_file_defining_one_enum_does_not_blank_the_others(self):
        # Replacement, not merge — safe ONLY because defaults live in code and apply per section.
        # A local file answering just send_mode must not wipe business_category.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "business_enums.local.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"send_mode": {"data_dictionary": {"0": "NEWLY_EXPLAINED"}}}, handle)
            with mock.patch.object(usecase_catalog.config, "BUSINESS_ENUMS_JSON", path):
                self.assertEqual(usecase_catalog.resolve_send_mode("0")["label"],
                                 "NEWLY_EXPLAINED")
                # business_category untouched, straight from the in-code seed.
                self.assertEqual(usecase_catalog.resolve_business_category("6")["source"],
                                 "data_dictionary")


class UnreadableSendModeVisibilityTest(unittest.TestCase):
    """Code 0 covers ~903 rows and nobody has explained it.

    The per-use-case cross-check correctly SKIPS a code it cannot read — it has no meaning to
    compare against. That is right, and it is also why a dataset-wide finding is needed: without
    one, those rows leave no trace anywhere, which is the same "silence reads as fine" shape this
    codebase keeps closing.
    """

    def _bucket(self, identities):
        pending, unexpected = {}, {}
        for identity in identities:
            resolved = usecase_catalog.resolve_send_mode(identity["send_mode_code"])
            if resolved["code"] is None or resolved["known"]:
                continue
            target = pending if resolved["pending"] else unexpected
            target[resolved["code"]] = target.get(resolved["code"], 0) + 1
        return pending, unexpected

    def test_pending_and_unexpected_codes_are_counted_separately(self):
        pending, unexpected = self._bucket([
            {"send_mode_code": "0"},    # pending — 903 real rows, meaning not supplied
            {"send_mode_code": "0"},
            {"send_mode_code": "99"},   # unexpected — genuine drift
            {"send_mode_code": "5"},    # defined since 2026-08-06 -> neither
            {"send_mode_code": "4"},    # defined since 2026-08-06 -> neither
            {"send_mode_code": ""},     # absent -> neither
        ])
        self.assertEqual(pending, {0: 2})
        self.assertEqual(unexpected, {99: 1})

    def test_the_cross_check_skips_a_code_it_cannot_read(self):
        ast = rt.parse("EMAIL & SMS")
        self.assertIsNone(ucc._send_mode_finding("X", ast, {"send_mode_code": "0"}, []))

    def test_codes_4_and_5_are_no_longer_unreadable(self):
        for code in ("4", "5"):
            self.assertTrue(usecase_catalog.resolve_send_mode(code)["known"], code)


class VendorVerificationSummaryTest(unittest.TestCase):
    """The aggregate the intranet asked for: the live-traffic/no-carrier rows must be visible in a
    report summary, not only by walking every router row."""

    def _channels(self, verdicts):
        return [{"authoritative_router": [
            {"matched": True, "vendor_blank_explained": {"holds": v}} for v in verdicts]}]

    def test_counts_split_three_ways(self):
        summary = delivery_chain._vendor_verification(
            self._channels([True, True, False, None]))
        self.assertEqual(summary["holds"], 2)
        self.assertEqual(summary["fails"], 1)
        self.assertEqual(summary["undecidable"], 1)
        self.assertEqual(summary["blank_vendor_rows"], 4)

    def test_headline_reports_a_count_and_refuses_to_license_an_inference(self):
        # Owner-decided 2026-08-06: these are NOT a data-quality exception. The routing rules skip
        # whole router families per use case, so a live route with no recorded carrier is an
        # expected shape — count it, do not flag it.
        summary = delivery_chain._vendor_verification(self._channels([False, True]))
        self.assertIn("1 matched router row", summary["headline"])
        self.assertIn("NOT a data-quality exception", summary["headline"])
        # The one inference that stays forbidden regardless.
        self.assertIn("NOT evidence of which carrier it is", summary["headline"])

    def test_headline_never_calls_the_rows_unexplained(self):
        summary = delivery_chain._vendor_verification(self._channels([False, False]))
        self.assertNotIn("unexplained", summary["headline"].lower())

    def test_rows_with_a_present_vendor_are_not_counted(self):
        channels = [{"authoritative_router": [{"matched": True}]}]
        self.assertEqual(delivery_chain._vendor_verification(channels)["blank_vendor_rows"], 0)

    def test_no_headline_when_nothing_was_checked(self):
        self.assertEqual(delivery_chain._vendor_verification([])["headline"], "")

    def test_report_surfaces_the_summary(self):
        import impact_report
        lines = impact_report.render_delivery_chain({
            "available": True, "channel_source": "declared", "by_channel": [],
            "vendor_verification": {"holds": 283, "fails": 244, "undecidable": 812,
                                    "blank_vendor_rows": 1339, "headline": ""},
        })
        text = chr(10).join(lines)
        self.assertIn("244 carrying traffic with no carrier recorded", text)
        self.assertIn("283 standby", text)
        self.assertIn("not a data-quality exception", text)


if __name__ == "__main__":
    unittest.main()

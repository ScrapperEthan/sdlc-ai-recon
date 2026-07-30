"""Phase 1 of the incident path: alert text -> repos -> topics -> use cases. No MCP, no logs.

The fixtures here are synthetic on purpose. index/ is gitignored, so the external side has no copy
of the real repo tags / message edges / use-case snapshot; the box runs this same file against the
real artefacts (see the RUNBOOK-57 verification steps).

The alert strings, however, are the REAL formats measured in RUNBOOK-55 — that is the part the
parser must not get wrong.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, incident, usecase_catalog

# Real alarm names from RUNBOOK-55's sample (repo ids are public names, no incident data).
ALARM_ECS = "prodECS_mc-hk-hase-batch-letter-postman-job_service_CPUUtilizationMINOR[80percent]"
ALARM_ECS_PREPROC = (
    "preprocECS_mc-hk-hase-batch-letter-tracking-job_service_MemoryUsageMINOR[80percent]")
ALARM_RDS = "preprocRDS_db0CPUUtilizationWARN[80percent]"
ALERT_SHP = "MDC Alert - General SHP API Error - mc-hk-hase-pfp-outbound-api"
ALERT_PATH = ("MDC Error Counts Alert every 5mins - Delivery Job - "
              "WPB Servicing Realtime High Risk Path")

REPOS = [
    "mc-hk-hase-batch-letter-postman-job",
    "mc-hk-hase-batch-letter-postman",          # prefix of the above — must not double-report
    "mc-hk-hase-batch-letter-tracking-job",
    "mc-hk-hase-pfp-outbound-api",
    "amet-mdc-hsbc-cm-outbound-job",
]

TOPIC_A = "hrn.hase.wpb.notification.servicing-realtime-highrisk-letter"
TOPIC_B = "hrn.hase.wpb.notification.marketing-batch-oeml"


class _Fixture(unittest.TestCase):
    """Writes a minimal but structurally real set of artefacts and points config at them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name

        edges = os.path.join(root, "message_edges.csv")
        with open(edges, "w", encoding="utf-8", newline="") as handle:
            handle.write("producer_repo,destination,consumer_repo,routing_source,evidence\n")
            handle.write(f"mc-hk-hase-batch-letter-postman-job,{TOPIC_A},,code,x.java:1\n")
            handle.write(f",{TOPIC_B},mc-hk-hase-batch-letter-postman-job,code,y.java:2\n")
            handle.write(f"mc-hk-hase-pfp-outbound-api,{TOPIC_A},,code,z.java:3\n")

        snapshot = os.path.join(root, "usecase.csv")
        with open(snapshot, "w", encoding="utf-8", newline="") as handle:
            handle.write("use_case_id,topic_name\n")     # line 1
            handle.write(f"C9508,{TOPIC_A}\n")           # line 2
            handle.write(f"C9509,{TOPIC_A}\n")           # line 3
            handle.write(f"C1000,{TOPIC_B}\n")           # line 4
            handle.write(f"C9508,{TOPIC_A}\n")           # line 5 (duplicate — must dedupe)

        tags = os.path.join(root, "repo_tags.json")
        with open(tags, "w", encoding="utf-8") as handle:
            json.dump({repo: {"channel": ["LETTER"], "business_line": "WPB",
                              "time_critical": False} for repo in REPOS}, handle)

        # A minimal legacy use-case master. Its presence is what makes active_dataset() resolve,
        # which is what makes the topic -> use-case route dimension AVAILABLE. Without it the
        # route dimension is correctly reported as uncomputable (see RouteDimensionGuardTests).
        master = os.path.join(root, "tbl_use_case.csv")
        with open(master, "w", encoding="utf-8", newline="") as handle:
            handle.write("use_case_id,use_case_name,source_system,status\n")
            handle.write("C9508,Letter servicing,MDC,Y\n")
            handle.write("C9509,Letter servicing 2,MDC,Y\n")
            handle.write("C1000,Marketing batch,PEGA,Y\n")

        patterns = os.path.join(root, "alarm_patterns.json")
        with open(patterns, "w", encoding="utf-8") as handle:
            json.dump({
                "environments": {"prefixes": ["preproc", "prod", "uat"]},
                "resource_types": {"tokens": ["ECS", "RDS"]},
                "severities": {"tokens": ["CRITICAL", "MAJOR", "MINOR", "WARNING", "WARN"]},
                "metrics": {"tokens": ["CPUUtilization", "MemoryUsage"]},
                "timezones": {"aliases": {"HKT": "Asia/Hong_Kong", "UTC": "UTC"}},
                "delivery_path": {"name_to_id": {}},
                "resource_repo_hints": {
                    "_note": "ignored",
                    "db0": ["mc-hk-hase-batch-letter-postman-job"],
                },
            }, handle)

        self._patches = [
            mock.patch.object(config, "MESSAGE_EDGES_CSV", edges),
            mock.patch.object(config, "USECASE_SNAPSHOT_CSV", snapshot),
            mock.patch.object(config, "REPO_TAGS_JSON", tags),
            mock.patch.object(config, "ALARM_PATTERNS_JSON", patterns),
            mock.patch.object(config, "USECASE_MASTER_CSV", master),
            mock.patch.object(config, "USECASE_DATASET_DIR", os.path.join(root, "no-manifest")),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()


class ParseAlertTests(_Fixture):
    def test_identifies_repo_embedded_in_real_ecs_alarm_name(self):
        parsed = incident.parse_alert(ALARM_ECS, repos=REPOS)
        self.assertTrue(parsed["identified"])
        self.assertEqual([r["repo"] for r in parsed["repos"]],
                         ["mc-hk-hase-batch-letter-postman-job"])
        self.assertEqual(parsed["repos"][0]["confidence"], "confirmed")

    def test_longer_repo_wins_so_a_prefix_repo_is_not_double_reported(self):
        # 'mc-hk-hase-batch-letter-postman' is a real (fixture) repo AND a prefix of the one in the
        # alarm. Reporting both would silently double an incident's blast radius.
        parsed = incident.parse_alert(ALARM_ECS, repos=REPOS)
        self.assertNotIn("mc-hk-hase-batch-letter-postman",
                         [r["repo"] for r in parsed["repos"]])

    def test_identifies_repo_in_free_text_alert_not_just_aws_format(self):
        parsed = incident.parse_alert(ALERT_SHP, repos=REPOS)
        self.assertEqual([r["repo"] for r in parsed["repos"]], ["mc-hk-hase-pfp-outbound-api"])

    def test_partial_token_is_not_a_match(self):
        parsed = incident.parse_alert("service mc-hk-hase-pfp-outbound-apixyz failed", repos=REPOS)
        self.assertFalse(parsed["identified"])

    def test_unidentifiable_alert_refuses_to_guess(self):
        parsed = incident.parse_alert("CMB Postman V3 failing", repos=REPOS)
        self.assertFalse(parsed["identified"])
        self.assertEqual(parsed["repos"], [])
        self.assertTrue(any("NOT guessing" in note for note in parsed["notes"]))

    def test_commentary_fields_from_the_knob_file(self):
        parsed = incident.parse_alert(ALARM_ECS_PREPROC, repos=REPOS)
        self.assertEqual(parsed["environment"], "preproc")
        self.assertEqual(parsed["resource_type"], "ECS")
        self.assertEqual(parsed["severity"], "MINOR")
        self.assertEqual(parsed["metric"], "MemoryUsage")
        self.assertEqual(parsed["threshold"], "80percent")

    def test_resource_hint_is_candidate_never_confirmed(self):
        parsed = incident.parse_alert(ALARM_RDS, repos=REPOS)
        self.assertTrue(parsed["identified"])
        self.assertEqual(parsed["repos"][0]["confidence"], "candidate")
        self.assertIn("hand-asserted", parsed["repos"][0]["why"])

    def test_delivery_path_phrase_is_reported_but_not_resolved(self):
        parsed = incident.parse_alert(ALERT_PATH, repos=REPOS)
        self.assertEqual(parsed["delivery_path"]["phrase"], "WPB Servicing Realtime High Risk Path")
        self.assertIsNone(parsed["delivery_path"]["resolved_id"])
        self.assertIn("numeric enum", parsed["delivery_path"]["note"])

    def test_time_without_a_timezone_is_flagged_ambiguous(self):
        parsed = incident.parse_alert("alarm fired at 12:25:08 on " + ALARM_ECS, repos=REPOS)
        self.assertTrue(any(t["ambiguous"] for t in parsed["times"]))
        self.assertTrue(any("three coexist" in n.lower() for n in parsed["notes"]))

    def test_time_with_a_timezone_resolves_and_is_not_flagged(self):
        parsed = incident.parse_alert("fired 12:25:08 HKT " + ALARM_ECS, repos=REPOS)
        stamps = [t for t in parsed["times"] if t["timezone"]]
        self.assertTrue(stamps)
        self.assertEqual(stamps[0]["timezone"], "Asia/Hong_Kong")
        self.assertFalse(stamps[0]["ambiguous"])

    def test_full_datetime_is_not_counted_twice_by_the_clock_pattern(self):
        parsed = incident.parse_alert("2026-07-28T04:25:08 UTC " + ALARM_ECS, repos=REPOS)
        self.assertEqual(len(parsed["times"]), 1)
        self.assertEqual(parsed["times"][0]["timezone"], "UTC")

    def test_no_repo_universe_says_so_instead_of_returning_nothing(self):
        parsed = incident.parse_alert(ALARM_ECS, repos=[])
        self.assertFalse(parsed["identified"])
        self.assertTrue(any("repo universe unavailable" in n for n in parsed["notes"]))


class BlastRadiusTests(_Fixture):
    def test_repo_reaches_use_cases_through_its_topics(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        self.assertTrue(out["available"])
        self.assertEqual(sorted(t["topic"] for t in out["topics"]), sorted([TOPIC_A, TOPIC_B]))
        self.assertEqual(sorted(u["use_case"] for u in out["use_cases"]),
                         ["C1000", "C9508", "C9509"])

    def test_duplicate_snapshot_rows_do_not_inflate_the_count(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        self.assertEqual(out["use_case_total"], 3)   # C9508 appears twice in the snapshot

    def test_direction_is_reported_per_topic(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        by_topic = {t["topic"]: t["direction"] for t in out["topics"]}
        self.assertEqual(by_topic[TOPIC_A], ["produce"])
        self.assertEqual(by_topic[TOPIC_B], ["consume"])

    def test_every_use_case_carries_a_snapshot_citation(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        self.assertTrue(all(item.get("citation") for item in out["use_cases"]))

    def test_vendor_is_refused_until_the_router_table_is_wired_in(self):
        """The refusal is the contract; the WORDING has to track reality. This assertion used to
        pin the literal phrase "not ingested", which turned into a false statement on the box the
        moment the intranet ingested the table (RUNBOOK-54, 247 rows) — the answer then told the
        reader to go fetch something they already had."""
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        self.assertIsNone(out["vendor"])
        self.assertIn("tbl_use_case_router", out["vendor_note"])
        self.assertIn("Do NOT infer a vendor from repo names", out["vendor_note"])

    def test_vendor_note_says_ingested_but_unwired_once_the_table_is_present(self):
        """The box's real state after RUNBOOK-54: table present, join not implemented."""
        with mock.patch.object(usecase_catalog, "active_dataset", return_value={
            "environment": "UAT", "snapshot_id": "20260727-1542", "tables": {
                "tbl_use_case_router": {"path": __file__, "row_count": 247,
                                         "exported_at": "2026-07-27"}}}):
            status = usecase_catalog.router_table_status()
        self.assertTrue(status["declared"])
        self.assertFalse(status["wired"])
        self.assertEqual(status["row_count"], 247)
        self.assertIn("IS ingested", status["note"])
        self.assertIn("NOT yet joined", status["note"])
        self.assertIn("not wired in yet", status["note"])

    def test_repo_with_no_edges_is_empty_not_an_error(self):
        out = incident.blast_radius("amet-mdc-hsbc-cm-outbound-job")
        self.assertTrue(out["available"])
        self.assertEqual(out["topics"], [])
        self.assertEqual(out["use_case_total"], 0)
        self.assertTrue(any("NOT 'affects nobody'" in c for c in out["caveats"]))


class IncidentImpactTests(_Fixture):
    def test_end_to_end_from_a_real_alarm_string(self):
        out = incident.incident_impact(ALARM_ECS, repos=REPOS)
        self.assertTrue(out["ok"])
        self.assertEqual(out["totals"]["repos"], 1)
        self.assertEqual(out["totals"]["use_cases_from_repos"], 3)
        self.assertEqual(out["affected"][0]["identified_via"], "confirmed")

    def test_unidentifiable_alert_fails_closed_with_a_next_step(self):
        out = incident.incident_impact("something broke", repos=REPOS)
        self.assertFalse(out["ok"])
        self.assertIn("could not identify", out["error"])
        self.assertIn("Do not guess", out["next_step"])

    def test_result_never_claims_to_have_read_production(self):
        out = incident.incident_impact(ALARM_ECS, repos=REPOS)
        self.assertIn("no MCP", out["source"])


if __name__ == "__main__":
    unittest.main()


# The real "MDC Alert - General SHP API Error" shape, read off the 2026-07-29 Task_Scope
# screenshots: no repo name anywhere, but the colleagues' analysis output quotes the use case.
ALERT_SHP_USECASE = (
    "Alert classification: MDC Alert - General SHP API Error, SMS, CSL outbound API; "
    "Payload history: payloadUuid=C6S03401259, useCase=[M2101] FPS Inward credit Success; "
    "Route=CSL_SVC_RT_SMS")


class UseCaseEntryPointTests(_Fixture):
    """The second way in: the biggest alert family names no repo, only a use case."""

    def test_use_case_id_in_the_alert_is_verified_against_the_snapshot(self):
        parsed = incident.parse_alert("useCase=[C9508] something", repos=REPOS)
        self.assertEqual([u["use_case"] for u in parsed["use_cases"]], ["C9508"])
        self.assertEqual(parsed["use_cases"][0]["topics"], [TOPIC_A])
        self.assertTrue(parsed["identified"])

    def test_an_id_shaped_string_that_is_not_a_real_use_case_is_refused(self):
        parsed = incident.parse_alert("error code A1234 on the gateway", repos=REPOS)
        self.assertEqual(parsed["use_cases"], [])
        self.assertFalse(parsed["identified"])
        self.assertTrue(any("not in the routing snapshot" in n for n in parsed["notes"]))

    def test_repo_less_alert_still_produces_impact_via_the_use_case(self):
        parsed = incident.parse_alert(ALERT_SHP_USECASE.replace("M2101", "C9508"), repos=REPOS)
        self.assertEqual(parsed["repos"], [])          # no repo id anywhere in the text
        self.assertTrue(parsed["identified"])          # but still identified, via the use case

    def test_use_case_route_reports_repos_as_candidate_not_confirmed(self):
        out = incident.blast_radius_for_use_case("C9508", [TOPIC_A])
        repos = {r["repo"]: r for r in out["repos"]}
        self.assertIn("mc-hk-hase-batch-letter-postman-job", repos)
        self.assertIn("mc-hk-hase-pfp-outbound-api", repos)
        for entry in repos.values():
            self.assertEqual(entry["confidence"], "candidate")
        self.assertTrue(any("not 'this repo failed'" in c for c in out["caveats"]))

    def test_end_to_end_counts_both_entry_points_separately(self):
        out = incident.incident_impact(
            ALARM_ECS + " useCase=[C9508]", repos=REPOS)
        self.assertTrue(out["ok"])
        self.assertEqual(out["totals"]["repos"], 1)
        self.assertEqual(out["totals"]["use_cases_named_in_alert"], 1)

    def test_still_fails_closed_when_neither_route_finds_anything(self):
        out = incident.incident_impact("CMB Postman V3 failing", repos=REPOS)
        self.assertFalse(out["ok"])
        self.assertIn("use case", out["error"])


class RouteDimensionGuardTests(_Fixture):
    """RUNBOOK-57 regression: three real alerts each returned 0 use cases with no explanation.

    Root cause was not the data — it was this module calling messages.reverse_lookup_use_cases()
    directly, bypassing the same-environment guard usecase_catalog already had (its "defect #2":
    UAT coverage computed off the stale dev/SCT route file). In an incident, a silent zero reads as
    "no business impact", which is the most dangerous direction to be wrong in.
    """

    def _no_route_dimension(self):
        """Remove the master CSV so active_dataset() -> None -> no route table."""
        return mock.patch.object(config, "USECASE_MASTER_CSV",
                                 os.path.join(self._tmp.name, "absent.csv"))

    def test_missing_route_table_says_cannot_compute_not_zero(self):
        with self._no_route_dimension():
            out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        link = out["use_case_link"]
        self.assertFalse(link["available"])
        self.assertIn("NOT computable", link["reason"])
        self.assertIn("NOT 'no use cases affected'", link["do_not_conclude"])

    def test_topics_and_channels_still_work_without_the_route_table(self):
        """The half that works must keep working — RUNBOOK-57 confirmed repo->topic->channel is
        solid on real data even where the use-case join is not."""
        with self._no_route_dimension():
            out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        self.assertEqual(len(out["topics"]), 2)
        self.assertEqual(out["channels"], ["LETTER"])

    def test_zero_matches_with_a_present_route_table_is_still_explained(self):
        """A repo whose topics simply are not in the route table must say so, citing the measured
        255-vs-20 topic mismatch, rather than reporting a bare zero."""
        out = incident.blast_radius("amet-mdc-hsbc-cm-outbound-job")   # no message edges at all
        link = out["use_case_link"]
        self.assertTrue(link["available"])
        self.assertEqual(link["matched"], 0)
        self.assertIn("NOT evidence of zero business impact", link["do_not_conclude"])

    def test_a_working_join_reports_matched_without_a_scare_note(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        link = out["use_case_link"]
        self.assertTrue(link["available"])
        self.assertEqual(link["matched"], 3)
        self.assertNotIn("do_not_conclude", link)

    def test_channel_upper_bound_is_always_labelled_as_an_upper_bound(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        bound = out["channel_upper_bound"]
        self.assertEqual(bound["method"], "channel_upper_bound")
        self.assertIn("UPPER BOUND", bound["caveat"])
        self.assertIn("at most", bound["caveat"])

    def test_caveats_tell_the_reader_to_check_the_link_before_quoting_a_number(self):
        out = incident.blast_radius("mc-hk-hase-batch-letter-postman-job")
        self.assertTrue(any("use_case_link" in c for c in out["caveats"]))

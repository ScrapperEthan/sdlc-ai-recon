import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import ExitStack
from unittest import mock
from xml.sax.saxutils import escape

import enrich_repo_tags
import make_repo_tags
import retrieval_service
from retriever import config as rconfig
from retriever import repo_tags


EDGES = """from_repo,to_repo
mc-hk-hase-svc-rt-alert-sms-api,mc-hk-hase-api-parent
amet-mdc-hsbc-batch-email-job,mc-hk-hase-api-parent
"""

BUNDLES = {
    "ingress": {"primary": ["mc-hk-hase-svc-rt-alert-sms-api"], "with_libs": ["mc-hk-hase-svc-rt-alert-sms-api"]},
    "email-batch": {"primary": ["amet-mdc-hsbc-batch-email-job"], "with_libs": ["amet-mdc-hsbc-batch-email-job"]},
}

ROUTE_TAGS = {
    "mc-hk-hase-svc-rt-alert-sms-api": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "realtime",
        "tokens": ["svc", "alert"],
        "bundle": "ingress",
    },
    "amet-mdc-hsbc-batch-email-job": {
        "system": "amet-mdc",
        "channel": ["email"],
        "mode": "batch",
        "tokens": ["hsbc"],
        "bundle": "email-batch",
    },
}


MDC_GROUP_TAGS = {
    # amet-mdc-* member via the name-derived `system` tag.
    "amet-mdc-hsbc-batch-email-job": {
        "system": "amet-mdc",
        "channel": ["email"],
        "mode": "batch",
    },
    # mc-hk-hase-* repo that is an MDC member ONLY via the business-sheet mdc_common flag — its
    # name has no "mdc" in it and its system tag is "hase", not "amet-mdc".
    "mc-hk-hase-sms-job": {
        "system": "hase",
        "channel": ["sms"],
        "mode": "realtime",
        "mdc_common": True,
    },
    # An ordinary hase repo with no MDC signal at all — must be excluded from both mdc_common
    # filtering and mdc_repos().
    "mc-hk-hase-other-job": {
        "system": "hase",
        "channel": ["push"],
        "mode": "realtime",
    },
}


MDC_HEADERS = [
    "Repository", "MDC Common", "SMS", "EMAIL", "PUSH", "WhatsAPP", "Letter", "Wechat",
    "Others", "Remark", "Batch/Realtime(B/R)", "Maraketing/Servicing(M/S)",
    "TimeCritcal(Y/N)", "CMB/WPB",
]


def write_fixture_xlsx(path):
    """Build a minimal stdlib-only XLSX fixture with sparse rows and shared strings."""
    rows = [
        MDC_HEADERS,
        ["mc-hk-hase-sms-job", "Y", "", "", "", "", "", "", "Y", "", "R", "S", "Y", "CMB"],
        ["lib", "Y", "", "", "", "", "", "", "Y", "", "", "", "", ""],
    ]
    strings = []
    for row in rows:
        for value in row:
            if value and value not in strings:
                strings.append(value)

    def cells(row_number, values):
        values_xml = []
        for index, value in enumerate(values):
            if not value:  # XLSX omits blank cells; parser must retain column identity.
                continue
            column = chr(ord("A") + index)
            values_xml.append(
                f'<c r="{column}{row_number}" t="s"><v>{strings.index(value)}</v></c>'
            )
        return f'<row r="{row_number}">{"".join(values_xml)}</row>'

    shared = "".join(f"<si><t>{escape(value)}</t></si>" for value in strings)
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{cells(1, rows[0])}{cells(2, rows[1])}{cells(3, rows[2])}</sheetData></worksheet>'
    )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="full Repository List" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>')
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


class MakeRepoTagsTests(unittest.TestCase):
    def _request_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))

    def _write_service_root(self, root, with_tags=True):
        index_dir = os.path.join(root, "index")
        os.makedirs(index_dir, exist_ok=True)
        if with_tags:
            with open(os.path.join(index_dir, "repo_tags.json"), "w", encoding="utf-8") as handle:
                json.dump(ROUTE_TAGS, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")

    def test_derivation_assigns_system_mode_channel_and_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            bundles = os.path.join(tmp, "bundles.json")
            override = os.path.join(tmp, "override.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump(BUNDLES, handle)
            with open(override, "w", encoding="utf-8") as handle:
                json.dump({}, handle)

            args = make_repo_tags.parse_args(
                ["--edges", edges, "--bundles", bundles, "--override", override, "--out", os.path.join(tmp, "out.json")]
            )
            payload = make_repo_tags.build_repo_tags(args)

            entry = payload["mc-hk-hase-svc-rt-alert-sms-api"]
            self.assertEqual(entry["system"], "hase")
            self.assertEqual(entry["mode"], "realtime")
            self.assertEqual(entry["channel"], ["sms"])
            self.assertEqual(entry["bundle"], "ingress")

    def test_repos_file_seeds_edgeless_repos_into_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            repos_file = os.path.join(tmp, "repos.txt")
            bundles = os.path.join(tmp, "bundles.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)
            # An isolated repo with no internal Maven edge, plus one already in the edges.
            with open(repos_file, "w", encoding="utf-8") as handle:
                handle.write("shp-pipeline-shared-lib-python\nmc-hk-hase-svc-rt-alert-sms-api\n")
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump({"pipeline": {"primary": ["shp-pipeline-shared-lib-python"]}}, handle)

            args = make_repo_tags.parse_args([
                "--edges", edges, "--repos-file", repos_file, "--bundles", bundles,
                "--override", os.path.join(tmp, "missing.json"),
                "--mdc", os.path.join(tmp, "missing-mdc.json"),
                "--out", os.path.join(tmp, "out.json"),
            ])
            payload = make_repo_tags.build_repo_tags(args)

            # The edge-less repo is now present, with name-derived tags, its frozen bundle,
            # and an honestly-empty serves_channels (nothing channel-owning depends on it).
            self.assertIn("shp-pipeline-shared-lib-python", payload)
            entry = payload["shp-pipeline-shared-lib-python"]
            self.assertEqual(entry["bundle"], "pipeline")
            self.assertEqual(entry["serves_channels"], [])
            # Edge-derived repos are unaffected.
            self.assertIn("amet-mdc-hsbc-batch-email-job", payload)

    def test_bundle_plan_seeds_universe_without_edges_or_repos_file(self):
        # Canonical universe = edges ∪ bundle plan (no --repos-file). Repos named only in the
        # frozen bundle plan get tagged even with no Maven edge, and nothing extra is invented.
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            bundles = os.path.join(tmp, "bundles.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump({
                    "pipeline": {
                        "primary": ["shp-pipeline-shared-lib-python"],
                        "with_libs": ["shp-pipeline-shared-lib-python", "shp-pipeline-configuration"],
                    },
                }, handle)

            args = make_repo_tags.parse_args([
                "--edges", edges, "--bundles", bundles,
                "--override", os.path.join(tmp, "missing.json"),
                "--mdc", os.path.join(tmp, "missing-mdc.json"),
                "--out", os.path.join(tmp, "out.json"),
            ])
            payload = make_repo_tags.build_repo_tags(args)

            self.assertIn("shp-pipeline-shared-lib-python", payload)   # bundle primary, no edge
            self.assertIn("shp-pipeline-configuration", payload)       # bundle with_libs, no edge
            self.assertEqual(payload["shp-pipeline-shared-lib-python"]["bundle"], "pipeline")
            self.assertEqual(payload["shp-pipeline-shared-lib-python"]["serves_channels"], [])
            # Universe is exactly edges (3 endpoints) ∪ bundle repos (2) — no scanned-dir extras.
            self.assertEqual(len(payload), 5)

    def test_msg_channels_folded_from_message_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            msg = os.path.join(tmp, "message_channels.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write("from_repo,to_repo\nmc-hk-hase-recon-report-job,lib\n")
            # a repo with NO name-derived channel gains one purely from its messaging config
            with open(msg, "w", encoding="utf-8") as handle:
                json.dump({"repos": {"mc-hk-hase-recon-report-job": {"channels": ["sms", "email", "other"]}}}, handle)
            args = make_repo_tags.parse_args([
                "--edges", edges, "--bundles", os.path.join(tmp, "no.json"),
                "--override", os.path.join(tmp, "no.json"), "--mdc", os.path.join(tmp, "no.json"),
                "--msg-channels", msg, "--out", os.path.join(tmp, "out.json"),
            ])
            payload = make_repo_tags.build_repo_tags(args)
            entry = payload["mc-hk-hase-recon-report-job"]
            self.assertEqual(entry["channel"], [])                       # still owns none
            self.assertEqual(entry["msg_channels"], ["email", "sms"])    # from messaging; "other" scrubbed
            metrics = dict(make_repo_tags.coverage_rows(payload))
            self.assertEqual(metrics["msg_channel_set"], 1)

    def test_override_merge_wins_over_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            bundles = os.path.join(tmp, "bundles.json")
            override = os.path.join(tmp, "override.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump(BUNDLES, handle)
            with open(override, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "mc-hk-hase-svc-rt-alert-sms-api": {
                            "channel": ["wechat"],
                            "mode": "batch",
                            "bundle": "manual-ingress",
                        }
                    },
                    handle,
                )

            args = make_repo_tags.parse_args(
                ["--edges", edges, "--bundles", bundles, "--override", override, "--out", os.path.join(tmp, "out.json")]
            )
            payload = make_repo_tags.build_repo_tags(args)

            entry = payload["mc-hk-hase-svc-rt-alert-sms-api"]
            self.assertEqual(entry["channel"], ["wechat"])
            self.assertEqual(entry["mode"], "batch")
            self.assertEqual(entry["bundle"], "manual-ingress")

    def test_others_override_neither_clobbers_channel_nor_pollutes_serves(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            override = os.path.join(tmp, "override.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write("from_repo,to_repo\nmc-hk-hase-sms-job,lib\n")
            with open(override, "w", encoding="utf-8") as handle:
                json.dump(
                    {"mc-hk-hase-sms-job": {"channel": ["others"]}, "lib": {"channel": ["others"]}},
                    handle,
                )
            args = make_repo_tags.parse_args([
                "--edges", edges, "--bundles", os.path.join(tmp, "missing.json"),
                "--override", override, "--mdc", os.path.join(tmp, "missing-mdc.json"),
                "--out", os.path.join(tmp, "out.json"),
            ])
            payload = make_repo_tags.build_repo_tags(args)
            # A ["others"] override must NOT clobber the name-derived channel or add a channel.
            self.assertEqual(payload["mc-hk-hase-sms-job"]["channel"], ["sms"])
            self.assertEqual(payload["lib"]["channel"], [])
            # "others" must never appear in serves_channels.
            self.assertEqual(payload["lib"]["serves_channels"], ["sms"])
            self.assertNotIn("others", payload["mc-hk-hase-sms-job"]["serves_channels"])

    def test_mdc_xlsx_enrichment_serves_channels_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheet = os.path.join(tmp, "MDC_Repo_List_Analysis.xlsx")
            write_fixture_xlsx(sheet)
            mdc, source_rows = enrich_repo_tags.parse_sheet(sheet)
            self.assertEqual(mdc["mc-hk-hase-sms-job"]["channel_declared"], ["other"])
            self.assertEqual(mdc["mc-hk-hase-sms-job"]["marketing_servicing"], "servicing")
            self.assertTrue(mdc["mc-hk-hase-sms-job"]["time_critical"])
            self.assertEqual(mdc["mc-hk-hase-sms-job"]["business_line"], "cmb")
            self.assertEqual(source_rows["lib"], 3)

            edges = os.path.join(tmp, "internal_edges.csv")
            mdc_path = os.path.join(tmp, "repo_tags.mdc.json")
            override = os.path.join(tmp, "override.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write("from_repo,to_repo\nmc-hk-hase-sms-job,lib\n")
            with open(mdc_path, "w", encoding="utf-8") as handle:
                json.dump(mdc, handle)
            with open(override, "w", encoding="utf-8") as handle:
                json.dump({}, handle)

            args = make_repo_tags.parse_args([
                "--edges", edges, "--bundles", os.path.join(tmp, "missing-bundles.json"),
                "--override", override, "--mdc", mdc_path, "--out", os.path.join(tmp, "repo_tags.json"),
            ])
            payload = make_repo_tags.build_repo_tags(args)
            self.assertEqual(payload["mc-hk-hase-sms-job"]["channel"], ["sms"])
            self.assertEqual(payload["mc-hk-hase-sms-job"]["channel_declared"], ["other"])
            self.assertEqual(payload["lib"]["serves_channels"], ["sms"])
            metrics = dict(make_repo_tags.coverage_rows(payload))
            self.assertEqual(metrics["serves_channel_set"], 2)
            self.assertEqual(metrics["channel_explained"], 1)

            report = enrich_repo_tags.reconcile(mdc, source_rows, payload)
            self.assertEqual(report["summary"]["mismatches"], 1)
            self.assertEqual(report["mismatches"][0]["repo"], "mc-hk-hase-sms-job")
            rendered = enrich_repo_tags.markdown_report(report)
            self.assertIn("MDC sheet:full Repository List row 2", rendered)

            tags_path = os.path.join(tmp, "repo_tags.json")
            report_md = os.path.join(tmp, "reports", "TAG_RECONCILE.md")
            report_json = os.path.join(tmp, "reports", "TAG_RECONCILE.json")
            make_repo_tags.write_payload(payload, tags_path)
            self.assertEqual(enrich_repo_tags.main([
                "--sheet", sheet, "--out", mdc_path, "--report", "--tags", tags_path,
                "--roster", os.path.join(tmp, "mdc_roster.json"),
                "--report-md", report_md, "--report-json", report_json,
            ]), 0)
            self.assertTrue(os.path.exists(report_md))
            self.assertTrue(os.path.exists(report_json))

    def test_filter_repos_query_is_case_insensitive_substring_on_repo_name(self):
        # "MDC" is both a repo-name family (amet-mdc-*) and an upstream source_system; filter_repos'
        # `system` field is exact-match ("amet-mdc"), so a user typing the bare word "mdc" needs the
        # substring `query` fallback to find the repo by NAME, not by its tagged system value.
        with tempfile.TemporaryDirectory() as tmp:
            tags_path = os.path.join(tmp, "repo_tags.json")
            with open(tags_path, "w", encoding="utf-8") as handle:
                json.dump(ROUTE_TAGS, handle)

            result = repo_tags.filter_repos(query="MDC", path=tags_path)
            self.assertEqual(result["repos"], ["amet-mdc-hsbc-batch-email-job"])
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["filters"]["query"], "MDC")

            # A substring that matches nothing real returns an empty (not erroring) result.
            empty = repo_tags.filter_repos(query="does-not-exist", path=tags_path)
            self.assertEqual(empty["repos"], [])
            self.assertEqual(empty["count"], 0)

    def test_filter_repos_query_combined_with_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tags_path = os.path.join(tmp, "repo_tags.json")
            with open(tags_path, "w", encoding="utf-8") as handle:
                json.dump(ROUTE_TAGS, handle)

            # query + matching mode narrows to the one repo.
            batch = repo_tags.filter_repos(query="mdc", mode="batch", path=tags_path)
            self.assertEqual(batch["repos"], ["amet-mdc-hsbc-batch-email-job"])

            # query + a mode that repo doesn't have narrows to nothing.
            realtime = repo_tags.filter_repos(query="mdc", mode="realtime", path=tags_path)
            self.assertEqual(realtime["repos"], [])

    def test_filter_repos_without_query_is_unaffected(self):
        # Back-compat: existing callers (retrieval_service._repos_payload) don't pass `query` —
        # behavior must be identical to before this parameter existed.
        with tempfile.TemporaryDirectory() as tmp:
            tags_path = os.path.join(tmp, "repo_tags.json")
            with open(tags_path, "w", encoding="utf-8") as handle:
                json.dump(ROUTE_TAGS, handle)

            result = repo_tags.filter_repos(system="hase", path=tags_path)
            self.assertEqual(result["repos"], ["mc-hk-hase-svc-rt-alert-sms-api"])
            self.assertEqual(result["filters"]["query"], "")

    def test_filter_repos_mdc_common_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tags_path = os.path.join(tmp, "repo_tags.json")
            with open(tags_path, "w", encoding="utf-8") as handle:
                json.dump(MDC_GROUP_TAGS, handle)

            result = repo_tags.filter_repos(mdc_common=True, path=tags_path)
            self.assertEqual(result["repos"], ["mc-hk-hase-sms-job"])
            self.assertEqual(result["count"], 1)
            self.assertTrue(result["filters"]["mdc_common"])

            # Back-compat: mdc_common=None (the default) filters nothing.
            unfiltered = repo_tags.filter_repos(path=tags_path)
            self.assertEqual(len(unfiltered["repos"]), 3)
            self.assertFalse(unfiltered["filters"]["mdc_common"])

    def test_mdc_repos_union_of_prefix_and_business_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tags_path = os.path.join(tmp, "repo_tags.json")
            with open(tags_path, "w", encoding="utf-8") as handle:
                json.dump(MDC_GROUP_TAGS, handle)

            result = repo_tags.mdc_repos(path=tags_path)
            self.assertEqual(result["group"], "mdc")
            self.assertEqual(result["count"], 2)
            by_repo = {item["repo"]: item["via"] for item in result["repos"]}
            self.assertEqual(by_repo["amet-mdc-hsbc-batch-email-job"], ["amet-mdc-prefix"])
            self.assertEqual(by_repo["mc-hk-hase-sms-job"], ["mdc_common"])
            self.assertNotIn("mc-hk-hase-other-job", by_repo)
            self.assertEqual(result["by_source"], {"amet-mdc": 1, "mdc_common": 1})

    def test_mdc_repos_missing_file_raises_not_swallowed(self):
        # mdc_repos uses missing_ok=False like filter_repos; the tools.dispatch layer (not this
        # function) is responsible for turning that into a clean {"ok": False} error.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                repo_tags.mdc_repos(path=os.path.join(tmp, "missing.json"))

    def test_repos_route_filters_and_missing_file_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_service_root(tmp, with_tags=True)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))

                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    status, payload = self._request_json(
                        f"http://{host}:{port}/repos?channel=sms&mode=realtime&system=hase&bundle=ingress"
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["repos"], ["mc-hk-hase-svc-rt-alert-sms-api"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

        with tempfile.TemporaryDirectory() as tmp:
            self._write_service_root(tmp, with_tags=False)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
                stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", os.path.join(tmp, "index")))
                stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "index", "repo_tags.json")))

                server = retrieval_service.create_server("127.0.0.1", 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host, port = server.server_address[:2]
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(f"http://{host}:{port}/repos?channel=sms", timeout=5)
                    self.assertEqual(caught.exception.code, 404)
                    payload = json.loads(caught.exception.read().decode("utf-8"))
                    self.assertEqual(payload["error"], "no repo_tags.json")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

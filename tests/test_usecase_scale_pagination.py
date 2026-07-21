"""Spec Tests section: "scale/UI: MDC-880 fixture -> pagination (offset/limit) works" — Building
block 8. The coverage-funnel/aggregate counts must stay full even when the `items` list returned to
an LLM/UI caller is capped, so a MDC-sized (~880 UC) source_system never overflows context by
default but a caller can still page through the whole list."""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

import impact_report
from retriever import config as rconfig
from webapp import tools

_COUNT = 880


def _build_mdc_dataset(root):
    dataset_dir = os.path.join(root, "index", "usecase-snapshots", "active")
    os.makedirs(dataset_dir, exist_ok=True)
    header = "use_case_id,use_case_name,source_system,status\n"
    rows = [f"MDC{i:04d},Use Case {i},MDC,Y\n" for i in range(_COUNT)]
    with open(os.path.join(dataset_dir, "tbl_use_case.snapshot.csv"), "w", encoding="utf-8", newline="") as handle:
        handle.write(header)
        handle.writelines(rows)
    manifest = {
        "environment": "UAT", "snapshot_id": "20260720-1730",
        "exported_at": "2026-07-20T17:30:00+08:00",
        "tables": {"tbl_use_case": {"file": "tbl_use_case.snapshot.csv", "row_count": _COUNT}},
    }
    with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    return dataset_dir


class MdcScalePaginationTests(unittest.TestCase):
    def _patch_all(self, stack, tmp, dataset_dir):
        stack.enter_context(mock.patch.object(rconfig, "USECASE_DATASET_DIR", dataset_dir))
        stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
        stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(tmp, "no-edges.csv")))
        stack.enter_context(mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(tmp, "no-msg.csv")))
        stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(tmp, "no-tags.json")))
        stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(tmp, "no-glossary.json")))

    def test_impact_report_default_returns_full_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = _build_mdc_dataset(tmp)
            with ExitStack() as stack:
                self._patch_all(stack, tmp, dataset_dir)
                report = impact_report.build_report("source-system:MDC")
        self.assertEqual(report["target"]["use_case_count"], _COUNT)
        self.assertEqual(report["use_cases"]["returned"], _COUNT)
        self.assertFalse(report["use_cases"]["truncated"])

    def test_tool_dispatch_defaults_to_top_50_not_a_full_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = _build_mdc_dataset(tmp)
            with ExitStack() as stack:
                self._patch_all(stack, tmp, dataset_dir)
                result = tools.dispatch("source_system_impact", {"source_system": "MDC"})
        # aggregate/funnel counts are always the FULL total — never silently truncated
        self.assertEqual(result["target"]["use_case_count"], _COUNT)
        self.assertEqual(result["target"]["coverage"]["catalog_only"], _COUNT)
        # but the items list defaults to a top-N sample so an LLM/UI response never overflows
        self.assertEqual(result["use_cases"]["returned"], 50)
        self.assertTrue(result["use_cases"]["truncated"])

    def test_offset_limit_pages_through_the_full_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = _build_mdc_dataset(tmp)
            seen_ids = set()
            offset = 0
            while offset < _COUNT:
                with ExitStack() as stack:
                    self._patch_all(stack, tmp, dataset_dir)
                    page = tools.dispatch("source_system_impact", {
                        "source_system": "MDC", "offset": offset, "limit": 200,
                    })
                ids = {item["use_case_id"] for item in page["use_cases"]["items"]}
                self.assertFalse(ids & seen_ids)  # no page overlap
                seen_ids |= ids
                offset += 200
        self.assertEqual(len(seen_ids), _COUNT)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from unittest import mock

import refresh
from retriever import config as rconfig

MASTER_HEADER = "use_case_id,source_system,business_category,work_stream_name,status\n"
MASTER_ROWS = "UC001,PEGA,11,streamA,Y\nUC002,,33,invalid,Y\n"


class UsecaseQualityStepTests(unittest.TestCase):
    def test_wires_into_refresh_as_additive_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = os.path.join(tmp, "index")
            os.makedirs(index_dir, exist_ok=True)
            master_path = os.path.join(index_dir, "tbl_use_case.snapshot.csv")
            with open(master_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(MASTER_HEADER)
                handle.write(MASTER_ROWS)

            with mock.patch.object(rconfig, "USECASE_MASTER_CSV", master_path):
                step = refresh.write_usecase_quality(index_dir)

            self.assertEqual(step["returncode"], 0)
            self.assertNotIn("error", step)
            md_path = os.path.join(index_dir, "reports", "USECASE_QUALITY.md")
            json_path = os.path.join(index_dir, "reports", "USECASE_QUALITY.json")
            self.assertTrue(os.path.exists(md_path))
            self.assertTrue(os.path.exists(json_path))
            with open(json_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertTrue(payload["available"])
            self.assertIn("33", payload["illegal_enum"]["codes"])
            self.assertEqual(payload["missing_source_system"]["count"], 1)

    def test_missing_snapshot_is_a_clean_skip_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = os.path.join(tmp, "index")
            os.makedirs(index_dir, exist_ok=True)
            with mock.patch.object(rconfig, "USECASE_MASTER_CSV", os.path.join(index_dir, "absent.csv")):
                step = refresh.write_usecase_quality(index_dir)
            self.assertEqual(step["returncode"], 0)
            self.assertIn("skipped", step)


if __name__ == "__main__":
    unittest.main()

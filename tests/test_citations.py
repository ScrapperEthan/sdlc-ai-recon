import os
import tempfile
import unittest
from unittest import mock

from retriever import citations, config


class CitationExtensionTests(unittest.TestCase):
    """csv/gradle/txt evidence files must be extractable and verifiable — the retrieval tools
    cite them (message_edges.csv, build.gradle, repos.txt) and before the regex covered these
    extensions the citation was silently dropped and never verified."""

    def _mirror_index(self, tmp):
        mirror = os.path.join(tmp, "mirror")
        index = os.path.join(tmp, "index")
        os.makedirs(os.path.join(mirror, "repoA"))
        os.makedirs(index)
        with open(os.path.join(mirror, "repoA", "build.gradle"), "w", encoding="utf-8") as f:
            f.write("plugins { id 'java' }\n")
        with open(os.path.join(index, "message_edges.csv"), "w", encoding="utf-8") as f:
            f.write("producer_repo,destination,consumer_repo\n")  # line 1
            f.write("a,topicX,b\n")                               # line 2
        return mirror, index

    def test_csv_and_gradle_citations_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror, index = self._mirror_index(tmp)
            with mock.patch.object(config, "MIRROR", mirror), \
                 mock.patch.object(config, "INDEX_DIR", index):
                citations._basename_index.cache_clear()
                report = citations.verify(
                    "evidence: repoA/build.gradle and index/message_edges.csv:2"
                )
        ok = {item["ref"]: item["ok"] for item in report["items"]}
        self.assertIn("repoA/build.gradle", ok)
        self.assertTrue(ok["repoA/build.gradle"])
        self.assertIn("index/message_edges.csv:2", ok)
        self.assertTrue(ok["index/message_edges.csv:2"])

    def test_csv_line_beyond_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror, index = self._mirror_index(tmp)
            with mock.patch.object(config, "MIRROR", mirror), \
                 mock.patch.object(config, "INDEX_DIR", index):
                citations._basename_index.cache_clear()
                report = citations.verify("index/message_edges.csv:99")
        item = report["items"][0]
        self.assertEqual(item["ref"], "index/message_edges.csv:99")
        self.assertFalse(item["ok"])


if __name__ == "__main__":
    unittest.main()

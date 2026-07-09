import os
import tempfile
import unittest

from retriever import glossary


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "example_glossary.json")


class GlossaryTests(unittest.TestCase):
    def test_expand_uses_fixture_glossary(self):
        self.assertEqual(
            glossary.expand("svc-rt", path=FIXTURE),
            "svc-rt (svc=servicing, rt=realtime)",
        )

    def test_absent_file_returns_input_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.json")
            self.assertEqual(glossary.expand("svc-rt", path=missing), "svc-rt")


if __name__ == "__main__":
    unittest.main()

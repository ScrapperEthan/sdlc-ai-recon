import os
import tempfile
import unittest
from unittest import mock

from retriever import code, config


class SearchCodeRepoNarrowingTests(unittest.TestCase):
    """search_code's `repos` param scopes the search to specific repo dirs under the mirror,
    instead of scanning the whole ~390-repo mirror. Exercises whichever backend (ripgrep or the
    stdlib walk fallback) is actually available in this environment."""

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_repos_param_narrows_matches_to_named_repo_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, "amet-mdc-hsbc-batch-email-job", "Foo.java"),
                        "class Foo { void NeedleXYZ() {} }\n")
            self._write(os.path.join(tmp, "mc-hk-hase-other-job", "Bar.java"),
                        "class Bar { void NeedleXYZ() {} }\n")

            with mock.patch.object(config, "MIRROR", tmp):
                # No narrowing: both repos' hits come back.
                everywhere = code.search_code("NeedleXYZ", glob="*.java")
                self.assertEqual(len(everywhere), 2)

                # Narrowed to just the mdc repo: only its hit comes back.
                narrowed = code.search_code("NeedleXYZ", glob="*.java",
                                             repos=["amet-mdc-hsbc-batch-email-job"])
                self.assertEqual(len(narrowed), 1)
                self.assertIn("amet-mdc-hsbc-batch-email-job", narrowed[0])
                self.assertNotIn("mc-hk-hase-other-job", narrowed[0])

    def test_repos_param_with_unknown_repo_falls_back_to_full_mirror(self):
        # A repo name that isn't a real directory under the mirror shouldn't crash or return
        # nothing silently forever — `roots` falls back to the whole mirror when nothing in
        # `repos` resolves to a real directory.
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, "mc-hk-hase-other-job", "Bar.java"),
                        "class Bar { void NeedleXYZ() {} }\n")
            with mock.patch.object(config, "MIRROR", tmp):
                result = code.search_code("NeedleXYZ", glob="*.java", repos=["does-not-exist"])
                self.assertEqual(len(result), 1)
                self.assertIn("mc-hk-hase-other-job", result[0])


if __name__ == "__main__":
    unittest.main()

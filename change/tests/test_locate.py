import unittest
from pathlib import Path

from change.intent import ChangeRequest
from change.locate import (
    AmbiguousTarget,
    _citation_report,
    _filter_verified_rationale,
    resolve_target,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
FIXTURE_MIRROR = FIXTURE_ROOT / "mirror"
FIXTURE_INDEX = FIXTURE_ROOT / "index"


class ResolveTargetTests(unittest.TestCase):
    def test_resolve_target_picks_repo_from_hint_and_keeps_verified_rationale(self):
        resolution = resolve_target(
            ChangeRequest("add_endpoint", "fixture service", "/status"),
            mirror=str(FIXTURE_MIRROR),
            index_dir=str(FIXTURE_INDEX),
        )

        self.assertEqual(resolution.repo, "mc-hk-hase-fixture-api")
        self.assertEqual(
            resolution.controller_path,
            "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java",
        )
        self.assertGreaterEqual(len(resolution.candidates), 2)
        self.assertTrue(resolution.rationale)
        self.assertTrue(any("index/REPOMAP.md" in sentence for sentence in resolution.rationale))
        for sentence in resolution.rationale:
            report = _citation_report(sentence, str(FIXTURE_MIRROR), str(FIXTURE_INDEX))
            self.assertEqual(report["total"], report["verified"])

    def test_bad_citation_line_is_dropped_from_rationale(self):
        good = (
            "Controller selection is grounded in the existing RestController at "
            "mc-hk-hase-fixture-api/src/main/java/com/example/fixture/api/demo/resource/DemoResource.java:7."
        )
        bad = (
            "Controller selection is grounded in the existing RestController at "
            "mc-hk-hase-fixture-api/src/main/java/com/example/fixture/api/demo/resource/DemoResource.java:9999."
        )

        self.assertEqual(
            _filter_verified_rationale([bad, good], str(FIXTURE_MIRROR), str(FIXTURE_INDEX)),
            [good],
        )

    def test_ambiguous_hint_refuses_instead_of_guessing(self):
        with self.assertRaises(AmbiguousTarget) as caught:
            resolve_target(
                ChangeRequest("add_endpoint", "api", "/status"),
                mirror=str(FIXTURE_MIRROR),
                index_dir=str(FIXTURE_INDEX),
            )

        repos = {candidate.repo for candidate in caught.exception.candidates}
        self.assertIn("mc-hk-hase-fixture-api", repos)
        self.assertIn("mc-hk-hase-other-api", repos)


if __name__ == "__main__":
    unittest.main()

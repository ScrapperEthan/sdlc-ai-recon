import tempfile
import unittest
from pathlib import Path

from change.from_intent import main
from change.locate import TARGET_FILE


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
FIXTURE_MIRROR = FIXTURE_ROOT / "mirror"
SERVICE = "mc-hk-hase-fixture-api"
OTHER_SERVICE = "mc-hk-hase-other-api"


def fake_build_ok(command, cwd):
    return 0, f"mocked {' '.join(command)} in {cwd}\nBUILD OK\n"


def runner_must_not_run(command, cwd):
    raise AssertionError("build runner must not be called")


class FromIntentTests(unittest.TestCase):
    def test_end_to_end_writes_resolution_diff_and_build_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"

            exit_code = main(
                [
                    "add a /status endpoint to the fixture service",
                    "--mirror",
                    str(FIXTURE_MIRROR),
                    "--out-dir",
                    str(out_dir),
                ],
                runner=fake_build_ok,
            )

            target = out_dir / f"{SERVICE}-change"
            source_controller = (
                FIXTURE_MIRROR
                / SERVICE
                / "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java"
            )
            target_controller = (
                target
                / "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java"
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((target / TARGET_FILE).exists())
            self.assertTrue((target / "CHANGE_DIFF.md").exists())
            self.assertTrue((target / "BUILD_RESULT.md").exists())
            self.assertIn('@GetMapping("/status")', target_controller.read_text(encoding="utf-8"))
            self.assertNotIn('@GetMapping("/status")', source_controller.read_text(encoding="utf-8"))

    def test_explain_only_writes_resolution_without_copy_or_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"

            exit_code = main(
                [
                    "add a /status endpoint to the fixture service",
                    "--mirror",
                    str(FIXTURE_MIRROR),
                    "--out-dir",
                    str(out_dir),
                    "--explain-only",
                ],
                runner=runner_must_not_run,
            )

            resolution = (out_dir / TARGET_FILE).read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertIn("Status: RESOLVED", resolution)
            self.assertIn("mc-hk-hase-fixture-api", resolution)
            self.assertFalse((out_dir / f"{SERVICE}-change").exists())

    def test_ambiguous_hint_writes_refusal_and_no_scratch_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"

            exit_code = main(
                [
                    "add a /status endpoint to the api",
                    "--mirror",
                    str(FIXTURE_MIRROR),
                    "--out-dir",
                    str(out_dir),
                ],
                runner=runner_must_not_run,
            )

            resolution = (out_dir / TARGET_FILE).read_text(encoding="utf-8")
            self.assertEqual(exit_code, 2)
            self.assertIn("Status: REFUSED", resolution)
            self.assertIn(SERVICE, resolution)
            self.assertIn(OTHER_SERVICE, resolution)
            self.assertFalse((out_dir / f"{SERVICE}-change").exists())
            self.assertFalse((out_dir / f"{OTHER_SERVICE}-change").exists())

    def test_invalid_endpoint_path_is_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"

            exit_code = main(
                [
                    "add a /../status endpoint to the fixture service",
                    "--mirror",
                    str(FIXTURE_MIRROR),
                    "--out-dir",
                    str(out_dir),
                ],
                runner=runner_must_not_run,
            )

            self.assertEqual(exit_code, 2)
            self.assertFalse(out_dir.exists())


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path

from change.add_endpoint import BuildFailed, add_endpoint, main


FIXTURE_MIRROR = Path(__file__).parent / "fixtures" / "mirror"
SERVICE = "mc-hk-hase-fixture-api"


def fake_build_ok(command, cwd):
    return 0, f"mocked {' '.join(command)} in {cwd}\nBUILD OK\n"


def fake_build_fail(command, cwd):
    return 23, f"mocked {' '.join(command)} in {cwd}\nBUILD FAILED\n"


class AddEndpointTests(unittest.TestCase):
    def test_adds_endpoint_test_and_review_artifacts_in_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            result = add_endpoint(
                SERVICE,
                "/status",
                mirror=str(FIXTURE_MIRROR),
                out_dir=str(out_dir),
                runner=fake_build_ok,
            )
            target = Path(result["path"])
            controller = target / "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java"
            source_controller = (
                FIXTURE_MIRROR
                / SERVICE
                / "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java"
            )
            test_file = target / "src/test/java/com/example/fixture/api/demo/resource/DemoResourceStatusTest.java"
            diff = (target / "CHANGE_DIFF.md").read_text(encoding="utf-8")
            build = (target / "BUILD_RESULT.md").read_text(encoding="utf-8")

            self.assertEqual(os.path.commonpath([str(out_dir.resolve()), result["path"]]), str(out_dir.resolve()))
            self.assertIn('@GetMapping("/status")', controller.read_text(encoding="utf-8"))
            self.assertIn("public String status()", controller.read_text(encoding="utf-8"))
            self.assertTrue(test_file.exists())
            self.assertIn("statusReturnsOk", test_file.read_text(encoding="utf-8"))
            self.assertNotIn('@GetMapping("/status")', source_controller.read_text(encoding="utf-8"))
            self.assertIn("Status: PASS (exit 0)", build)
            self.assertIn("BUILD OK", build)
            self.assertEqual(
                result["changed_files"],
                [
                    "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java",
                    "src/test/java/com/example/fixture/api/demo/resource/DemoResourceStatusTest.java",
                ],
            )
            self.assertIn("- src/main/java/com/example/fixture/api/demo/resource/DemoResource.java", diff)
            self.assertIn("- src/test/java/com/example/fixture/api/demo/resource/DemoResourceStatusTest.java", diff)
            self.assertNotIn("- pom.xml", diff)
            self.assertNotIn("- BUILD_RESULT.md", diff)

    def test_skip_build_generates_diff_without_running_the_build(self):
        def runner_must_not_run(command, cwd):
            raise AssertionError("build runner must not be called with skip_build")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            result = add_endpoint(
                SERVICE,
                "/status",
                mirror=str(FIXTURE_MIRROR),
                out_dir=str(out_dir),
                skip_build=True,
                runner=runner_must_not_run,
            )
            target = Path(result["path"])
            controller = target / "src/main/java/com/example/fixture/api/demo/resource/DemoResource.java"
            build = (target / "BUILD_RESULT.md").read_text(encoding="utf-8")

            self.assertIsNone(result["build"])
            self.assertIn('@GetMapping("/status")', controller.read_text(encoding="utf-8"))
            self.assertTrue((target / "CHANGE_DIFF.md").exists())
            self.assertIn("Status: SKIPPED", build)

    def test_rejects_path_escape_before_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            with self.assertRaises(ValueError):
                add_endpoint(
                    "../escape",
                    "/status",
                    mirror=str(FIXTURE_MIRROR),
                    out_dir=str(out_dir),
                    runner=fake_build_ok,
                )
            self.assertFalse(out_dir.exists())

            with self.assertRaises(ValueError):
                add_endpoint(
                    SERVICE,
                    "/../status",
                    mirror=str(FIXTURE_MIRROR),
                    out_dir=str(out_dir),
                    runner=fake_build_ok,
                )
            self.assertFalse(out_dir.exists())

    def test_failing_build_writes_result_and_cli_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            exit_code = main(
                [
                    SERVICE,
                    "--path",
                    "/status",
                    "--mirror",
                    str(FIXTURE_MIRROR),
                    "--out-dir",
                    str(out_dir),
                ],
                runner=fake_build_fail,
            )
            target = out_dir / f"{SERVICE}-change"
            build = (target / "BUILD_RESULT.md").read_text(encoding="utf-8")

            self.assertEqual(exit_code, 23)
            self.assertTrue((target / "CHANGE_DIFF.md").exists())
            self.assertIn("Status: FAIL (exit 23)", build)
            self.assertIn("BUILD FAILED", build)

    def test_function_raises_build_failed_with_result_after_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "scratch"
            with self.assertRaises(BuildFailed) as caught:
                add_endpoint(
                    SERVICE,
                    "/status",
                    mirror=str(FIXTURE_MIRROR),
                    out_dir=str(out_dir),
                    runner=fake_build_fail,
                )

            target = Path(caught.exception.result["path"])
            self.assertTrue((target / "CHANGE_DIFF.md").exists())
            self.assertTrue((target / "BUILD_RESULT.md").exists())


if __name__ == "__main__":
    unittest.main()


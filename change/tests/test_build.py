import unittest
from unittest import mock

from change.build import BuildResult, _resolve_command, run_maven_tests


class ResolveCommandTests(unittest.TestCase):
    def test_resolves_executable_via_which(self):
        with mock.patch("change.build.shutil.which", return_value=r"C:\tools\mvn.cmd") as which:
            resolved = _resolve_command(("mvn", "-q", "test"))
        which.assert_called_with("mvn")
        self.assertEqual(resolved, (r"C:\tools\mvn.cmd", "-q", "test"))

    def test_falls_back_to_original_when_not_found(self):
        with mock.patch("change.build.shutil.which", return_value=None):
            resolved = _resolve_command(("mvn", "-q", "test"))
        self.assertEqual(resolved, ("mvn", "-q", "test"))


class RunMavenTestsLaunchTests(unittest.TestCase):
    def test_missing_binary_is_recorded_as_failure_not_raised(self):
        # A binary that cannot exist on PATH: the launch must not crash the run.
        result = run_maven_tests(
            ".",
            command=("mvn-definitely-not-a-real-binary-xyz", "-q", "test"),
        )
        self.assertIsInstance(result, BuildResult)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not launch build command", result.output)


if __name__ == "__main__":
    unittest.main()

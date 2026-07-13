import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import clone_mirror


class CloneMirrorTests(unittest.TestCase):
    def test_names_from_repo_tags_and_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tags = os.path.join(tmp, "repo_tags.json")
            bundles = os.path.join(tmp, "bundles.json")
            with open(tags, "w", encoding="utf-8") as handle:
                json.dump({"repo-b": {"channel": []}, "repo-a": {"channel": []}}, handle)
            with open(bundles, "w", encoding="utf-8") as handle:
                json.dump({"ingress": {"primary": ["repo-a"], "with_libs": ["lib-x"]}}, handle)
            self.assertEqual(clone_mirror._names_from_json(tags), ["repo-a", "repo-b"])
            self.assertEqual(clone_mirror._names_from_json(bundles), ["lib-x", "repo-a"])

    def test_load_prefers_repos_file_then_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos_file = os.path.join(tmp, "repos.txt")
            with open(repos_file, "w", encoding="utf-8") as handle:
                handle.write("# comment\nrepo-1\nrepo-2\n")
            args = clone_mirror.parse_args(["--repos-file", repos_file])
            self.assertEqual(clone_mirror.load_repo_names(args), ["repo-1", "repo-2"])

    def test_repo_url_https_and_ssh_templates(self):
        self.assertEqual(
            clone_mirror.repo_url("mc-hk-hase-ingress-api", "https://host", "hase-mc"),
            "https://host/hase-mc/mc-hk-hase-ingress-api.git",
        )
        self.assertEqual(
            clone_mirror.repo_url("r", "ignored", "hase-mc", "git@host:{org}/{repo}.git"),
            "git@host:hase-mc/r.git",
        )

    def test_is_cloned_requires_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(clone_mirror.is_cloned(tmp))
            os.makedirs(os.path.join(tmp, ".git"))
            self.assertTrue(clone_mirror.is_cloned(tmp))

    def test_clone_one_skips_present_and_reports_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = os.path.join(tmp, "mirror")
            os.makedirs(os.path.join(mirror, "already", ".git"))
            self.assertEqual(clone_mirror.clone_one("already", mirror, "url", 5), ("skip", ""))

            def fake_ok(cmd, **kwargs):
                os.makedirs(os.path.join(cmd[-1], ".git"))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch("clone_mirror.subprocess.run", side_effect=fake_ok):
                self.assertEqual(clone_mirror.clone_one("fresh", mirror, "url", 5), ("cloned", ""))

            def fake_fail(cmd, **kwargs):
                return subprocess.CompletedProcess(cmd, 128, "", "auth failed")

            with mock.patch("clone_mirror.subprocess.run", side_effect=fake_fail):
                status, err = clone_mirror.clone_one("nope", mirror, "url", 5)
            self.assertEqual(status, "failed")
            self.assertIn("auth failed", err)
            self.assertFalse(os.path.isdir(os.path.join(mirror, "nope")))  # partial cleaned up


if __name__ == "__main__":
    unittest.main()

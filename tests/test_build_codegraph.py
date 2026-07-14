import io
import json
import os
import contextlib
import tempfile
import unittest
from unittest import mock

import build_codegraph


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


BUNDLES = {
    # not in PLAN_ORDER -> sorts after the priority bundles, alphabetically
    "zeta": {"primary": ["repo-z"], "with_libs": []},
    "ingress": {"primary": ["repo-a", "repo-b"], "with_libs": ["lib-x"]},
    "tracking": {"primary": ["repo-a"], "with_libs": []},
    "platform-core": {"primary": ["lib-x"], "with_libs": []},
}


class PlanTests(unittest.TestCase):
    def test_plan_order_and_present_missing_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = os.path.join(tmp, "mirror")
            for repo in ("repo-a", "lib-x"):  # repo-b, repo-z absent
                os.makedirs(os.path.join(mirror, repo))

            rows = build_codegraph.plan(BUNDLES, mirror)

            # PLAN_ORDER first (ingress, tracking, platform-core), then the rest alphabetically.
            self.assertEqual(
                [row["bundle"] for row in rows],
                ["ingress", "tracking", "platform-core", "zeta"],
            )
            ingress = rows[0]
            self.assertEqual(ingress["present"], ["repo-a", "lib-x"])
            self.assertEqual(ingress["missing"], ["repo-b"])
            self.assertEqual(ingress["present_count"], 2)
            self.assertEqual(ingress["missing_count"], 1)
            # zeta has no present repos -> present_count 0 (build_all will skip it).
            self.assertEqual(rows[-1]["present_count"], 0)

    def test_bundle_repos_dedupes_libs_into_primary(self):
        self.assertEqual(
            build_codegraph.bundle_repos({"primary": ["a", "b"], "with_libs": ["b", "c"]}),
            ["a", "b", "c"],
        )

    def test_only_filters_to_one_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = build_codegraph.plan(BUNDLES, tmp, only="tracking")
            self.assertEqual([row["bundle"] for row in rows], ["tracking"])

    def test_merge_manifest_upserts_and_preserves_other_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "codegraph_build.json")
            _write_json(manifest, {"bundles": [
                {"bundle": "ingress", "returncode": 0, "db_mib": 120},
                {"bundle": "tracking", "returncode": 0, "db_mib": 130},
            ]})
            # An `--only tracking`-style run supplies just tracking; ingress must survive.
            merged = build_codegraph.merge_manifest_bundles(
                manifest, [{"bundle": "tracking", "returncode": 0, "db_mib": 137}]
            )
            names = [entry["bundle"] for entry in merged]
            self.assertEqual(names, ["ingress", "tracking"])          # ingress preserved
            self.assertEqual(merged[1]["db_mib"], 137)                # tracking upserted
            # No prior manifest -> just the new entries.
            self.assertEqual(
                build_codegraph.merge_manifest_bundles(os.path.join(tmp, "nope.json"),
                                                       [{"bundle": "x", "returncode": 0}]),
                [{"bundle": "x", "returncode": 0}],
            )

    def test_reconcile_rebuilds_manifest_from_built_dirs_without_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_root = os.path.join(tmp, "codegraph")
            for name in ("ingress", "tracking"):
                dbdir = os.path.join(out_root, name, ".codegraph")
                os.makedirs(dbdir)
                with open(os.path.join(dbdir, "codegraph.db"), "wb") as handle:
                    handle.write(b"x" * 2048)
            os.makedirs(os.path.join(out_root, "not-built"))  # no .codegraph -> ignored
            manifest = os.path.join(tmp, "codegraph_build.json")

            code = build_codegraph.main(["--reconcile", "--out-root", out_root, "--manifest", manifest])

            self.assertEqual(code, 0)
            data = json.load(open(manifest, encoding="utf-8"))
            names = sorted(b["bundle"] for b in data["bundles"])
            self.assertEqual(names, ["ingress", "tracking"])
            self.assertTrue(all(b["returncode"] == 0 and b["reconciled"] for b in data["bundles"]))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = os.path.join(tmp, "mirror")
            os.makedirs(os.path.join(mirror, "repo-a"))
            bundles = os.path.join(tmp, "index", "bundles.json")
            manifest = os.path.join(tmp, "index", "codegraph_build.json")
            _write_json(bundles, BUNDLES)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = build_codegraph.main(
                    ["--dry-run", "--mirror", mirror, "--bundles", bundles, "--manifest", manifest]
                )

            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(manifest))
            self.assertIn("dry run", stdout.getvalue())


class StageTests(unittest.TestCase):
    def test_stage_copies_sources_excludes_git_and_target_and_inits_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = os.path.join(tmp, "mirror")
            repo = os.path.join(mirror, "repo-a")
            os.makedirs(os.path.join(repo, ".git"))
            os.makedirs(os.path.join(repo, "target"))
            os.makedirs(os.path.join(repo, "src"))
            with open(os.path.join(repo, ".git", "HEAD"), "w") as handle:
                handle.write("ref: refs/heads/main")
            with open(os.path.join(repo, "target", "out.class"), "w") as handle:
                handle.write("compiled")
            with open(os.path.join(repo, "src", "App.java"), "w") as handle:
                handle.write("class App {}")

            out_root = os.path.join(tmp, "codegraph")

            # git init is mocked to create the .git dir — proves stage_bundle inits the root
            # without needing a real git binary (and never touches the real codegraph).
            def fake_git(cmd, cwd=None, **kwargs):
                if cmd[:2] == ["git", "init"]:
                    os.makedirs(os.path.join(cwd, ".git"), exist_ok=True)
                import subprocess as sp

                return sp.CompletedProcess(cmd, 0, "", "")

            with mock.patch("build_codegraph.subprocess.run", side_effect=fake_git) as run:
                staging_root = build_codegraph.stage_bundle("ingress", ["repo-a"], mirror, out_root)

            staged_repo = os.path.join(staging_root, "repo-a")
            self.assertTrue(os.path.isfile(os.path.join(staged_repo, "src", "App.java")))
            self.assertFalse(os.path.isdir(os.path.join(staged_repo, ".git")))
            self.assertFalse(os.path.isdir(os.path.join(staged_repo, "target")))
            self.assertTrue(os.path.isdir(os.path.join(staging_root, ".git")))
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][:2], ["git", "init"])

    def test_stage_is_rerunnable_removes_stale_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = os.path.join(tmp, "mirror")
            os.makedirs(os.path.join(mirror, "repo-a", "src"))
            out_root = os.path.join(tmp, "codegraph")
            stale = os.path.join(out_root, "ingress", "old-leftover")
            os.makedirs(stale)

            with mock.patch("build_codegraph.subprocess.run"):
                build_codegraph.stage_bundle("ingress", ["repo-a"], mirror, out_root)

            self.assertFalse(os.path.isdir(stale))
            self.assertTrue(os.path.isdir(os.path.join(out_root, "ingress", "repo-a")))


if __name__ == "__main__":
    unittest.main()

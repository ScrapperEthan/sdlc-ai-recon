import contextlib
import io
import json
import os
import tempfile
import unittest

import make_bundles


EDGES = """from_repo,to_repo
mc-hk-hase-ingress-api,mc-hk-hase-api-parent
mc-hk-hase-ingress-job,mc-hk-hase-ingress-api
mc-hk-hase-svc-rt-alpha-api,mc-hk-hase-api-parent
mc-hk-hase-svc-rt-beta-job,mc-hk-hase-svc-rt-alpha-api
mc-hk-hase-foo-tracking-job,mc-hk-hase-api-parent
mc-hk-hase-bar-api,mc-hk-hase-api-common
mc-hk-hase-baz-job,mc-hk-hase-api-common
"""


class MakeBundlesTests(unittest.TestCase):
    def test_build_plan_assigns_primary_and_tracking_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)

            args = make_bundles.parse_args(
                [
                    "--edges",
                    edges,
                    "--merge-min",
                    "2",
                    "--pom-only-repo",
                    "mc-hk-hase-shp-infra",
                ]
            )
            payload, rows, coverage, total_repos = make_bundles.build_plan(args)

            self.assertEqual(coverage, total_repos)
            self.assertEqual(
                payload["platform-core"]["primary"],
                [
                    "mc-hk-hase-api-common",
                    "mc-hk-hase-api-parent",
                    "mc-hk-hase-shp-infra",
                ],
            )
            self.assertEqual(
                payload["ingress"]["primary"],
                ["mc-hk-hase-ingress-api", "mc-hk-hase-ingress-job"],
            )
            self.assertEqual(
                payload["svc-rt"]["primary"],
                ["mc-hk-hase-svc-rt-alpha-api", "mc-hk-hase-svc-rt-beta-job"],
            )
            self.assertEqual(
                payload["misc-bar-to-foo"]["primary"],
                [
                    "mc-hk-hase-bar-api",
                    "mc-hk-hase-baz-job",
                    "mc-hk-hase-foo-tracking-job",
                ],
            )
            self.assertEqual(payload["tracking"]["primary"], ["mc-hk-hase-foo-tracking-job"])
            self.assertIn("mc-hk-hase-api-parent", payload["ingress"]["with_libs"])
            self.assertEqual(rows[0]["bundle"], "misc-bar-to-foo")
            self.assertTrue(any(row["bundle"] == "platform-core" for row in rows))

    def test_main_writes_json_and_prints_review_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            out = os.path.join(tmp, "index", "bundles.json")
            with open(edges, "w", encoding="utf-8", newline="") as handle:
                handle.write(EDGES)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = make_bundles.main(
                    [
                        "--edges",
                        edges,
                        "--out",
                        out,
                        "--merge-min",
                        "2",
                        "--max-repos",
                        "1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertIn("tracking", payload)
            self.assertIn("repos>1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, repo_tags, unified_impact


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


class BundleRootForTests(unittest.TestCase):
    def _fixture(self, tmp):
        """A manifest with one clean build (ingress) and one failed build (tracking)."""
        ingress_root = os.path.join(tmp, "codegraph", "ingress")
        os.makedirs(os.path.join(ingress_root, "mc-hk-hase-ingress-api"))  # a staged repo dir
        manifest = os.path.join(tmp, "codegraph_build.json")
        _write_json(
            manifest,
            {
                "generated_at": "2026-07-13T00:00:00Z",
                "bundles": [
                    {"bundle": "ingress", "root": ingress_root, "returncode": 0},
                    {"bundle": "tracking", "root": os.path.join(tmp, "x"), "returncode": 1},
                    {"bundle": "empty", "skipped": "no repos in mirror", "missing_count": 3},
                ],
            },
        )
        tags = os.path.join(tmp, "repo_tags.json")
        _write_json(tags, {"mc-hk-hase-ingress-api": {"bundle": "ingress"}})
        return manifest, tags, ingress_root

    def test_resolves_explicit_bundle_then_repo_tag_then_dir_then_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, tags, ingress_root = self._fixture(tmp)
            with mock.patch.object(config, "CODEGRAPH_BUILD_JSON", manifest), mock.patch.object(
                config, "REPO_TAGS_JSON", tags
            ):
                # (1) explicit bundle arg that is built
                self.assertEqual(
                    unified_impact.bundle_root_for("AnySymbol", bundle="ingress"), ingress_root
                )
                # (2) seed is a repo -> routes by its repo_tags bundle
                self.assertEqual(
                    unified_impact.bundle_root_for("mc-hk-hase-ingress-api"), ingress_root
                )
                # (3) symbol seed matched to the built staging dir that contains the repo name
                self.assertEqual(
                    unified_impact.bundle_root_for("mc-hk-hase-ingress-api", bundle="unbuilt"),
                    ingress_root,
                )
                # explicit bundle that only failed to build -> not a root; falls through to None
                self.assertIsNone(unified_impact.bundle_root_for("nope", bundle="tracking"))
                # unroutable seed -> None (caller falls back to process cwd)
                self.assertIsNone(unified_impact.bundle_root_for("TotallyUnknownSymbol"))

    def test_bare_symbol_routes_via_defining_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, tags, ingress_root = self._fixture(tmp)
            # A search hit for the symbol, defined in the ingress repo's IngressService.java.
            hit = os.path.join(
                config.MIRROR, "mc-hk-hase-ingress-api", "src", "IngressService.java"
            ) + ":12:public class IngressService {"
            with mock.patch.object(config, "CODEGRAPH_BUILD_JSON", manifest), \
                 mock.patch.object(config, "REPO_TAGS_JSON", tags), \
                 mock.patch.object(unified_impact.code, "search_code", return_value=[hit]):
                # bare symbol, no bundle, not a repo, no matching staging dir -> resolved by the
                # repo that DEFINES it, so who-calls routes to a built index instead of grep.
                self.assertEqual(unified_impact.bundle_root_for("IngressService"), ingress_root)

    def test_no_manifest_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "codegraph_build.json")
            with mock.patch.object(config, "CODEGRAPH_BUILD_JSON", missing):
                self.assertIsNone(unified_impact.bundle_root_for("x", bundle="ingress"))

    def test_call_graph_falls_back_cleanly_without_codegraph(self):
        with mock.patch.object(unified_impact.shutil, "which", return_value=None), mock.patch.object(
            unified_impact.code, "search_code", return_value=["hit-1"]
        ), mock.patch.object(
            unified_impact, "_index_freshness", return_value=("2026-07-01T00:00:00Z", None, False)
        ):
            result = unified_impact._call_graph("Seed", cwd="/some/root")
            self.assertFalse(result["available"])
            self.assertEqual(result["bundle_root"], "/some/root")
            self.assertEqual(result["fallback_hits"], ["hit-1"])
            self.assertEqual(result["caveat"], unified_impact.CALL_GRAPH_CAVEAT)
            self.assertEqual(result["index_built_at"], "2026-07-01T00:00:00Z")
            self.assertIsNone(result["indexes_refreshed_at"])
            self.assertFalse(result["possibly_stale"])

    def test_call_graph_success_keeps_existing_fields_and_adds_trust_signals(self):
        completed = mock.Mock(returncode=0, stdout="callers", stderr="")
        with mock.patch.object(unified_impact.shutil, "which", return_value="codegraph"), \
             mock.patch.object(unified_impact.subprocess, "run", return_value=completed), \
             mock.patch.object(
                 unified_impact,
                 "_index_freshness",
                 return_value=("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", False),
             ):
            result = unified_impact._call_graph("Seed", cwd="/some/root")

        self.assertTrue(result["available"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["bundle_root"], "/some/root")
        self.assertEqual(result["output"], "callers")
        self.assertEqual(result["fallback_hits"], [])
        self.assertEqual(result["caveat"], unified_impact.CALL_GRAPH_CAVEAT)
        self.assertEqual(result["index_built_at"], "2026-07-01T00:00:00Z")
        self.assertEqual(result["indexes_refreshed_at"], "2026-07-01T00:00:00Z")
        self.assertFalse(result["possibly_stale"])

    def test_call_graph_exception_adds_trust_signals(self):
        with mock.patch.object(unified_impact.shutil, "which", return_value="codegraph"), \
             mock.patch.object(unified_impact.subprocess, "run", side_effect=RuntimeError("boom")), \
             mock.patch.object(unified_impact.code, "search_code", return_value=["hit-1"]), \
             mock.patch.object(
                 unified_impact,
                 "_index_freshness",
                 return_value=(None, "2026-07-01T00:00:00Z", False),
             ):
            result = unified_impact._call_graph("Seed", cwd="/some/root")

        self.assertFalse(result["available"])
        self.assertEqual(result["bundle_root"], "/some/root")
        self.assertEqual(result["error"], "boom")
        self.assertEqual(result["fallback_hits"], ["hit-1"])
        self.assertEqual(result["caveat"], unified_impact.CALL_GRAPH_CAVEAT)
        self.assertIsNone(result["index_built_at"])
        self.assertEqual(result["indexes_refreshed_at"], "2026-07-01T00:00:00Z")
        self.assertFalse(result["possibly_stale"])


class IndexFreshnessTests(unittest.TestCase):
    def test_uses_real_mtimes_and_marks_newer_derived_indexes_possibly_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, ".codegraph", "codegraph.db")
            os.makedirs(os.path.dirname(database))
            with open(database, "w", encoding="utf-8"):
                pass
            built_epoch = 1_700_000_000
            os.utime(database, (built_epoch, built_epoch))
            indexed = os.path.join(tmp, "index", "last_indexed.json")
            _write_json(indexed, {"generated_at": "2023-11-14T22:13:21Z"})
            with mock.patch.object(config, "INDEX_DIR", os.path.dirname(indexed)):
                self.assertEqual(
                    unified_impact._index_freshness(tmp),
                    ("2023-11-14T22:13:20Z", "2023-11-14T22:13:21Z", True),
                )

    def test_returns_unknown_timestamps_without_guessing_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "INDEX_DIR", tmp):
                self.assertEqual(unified_impact._index_freshness(None), (None, None, False))


class QueryDepResolutionTests(unittest.TestCase):
    """query() must route the dependency/message sections through the repo that defines a bare
    symbol — otherwise those sections come back empty because they key on repo, not symbol."""

    def test_symbol_seed_routes_deps_and_messages_to_defining_repo(self):
        hit = os.path.join(config.MIRROR, "repoA", "src", "Foo.java") + ":3:class Foo {"
        with mock.patch.object(unified_impact, "_known_repos", return_value={"repoA", "repoB"}), \
             mock.patch.object(unified_impact.code, "search_code", return_value=[hit]), \
             mock.patch.object(unified_impact, "bundle_root_for", return_value=None), \
             mock.patch.object(unified_impact, "_call_graph", return_value={"available": False}), \
             mock.patch.object(
                 unified_impact.graph, "impact",
                 return_value={"mode": "direct", "depended_on_by": ["repoB"], "depends_on": []},
             ) as impact, \
             mock.patch.object(
                 unified_impact, "_message_peers", return_value=[{"peer_repo": "repoB"}]
             ) as peers:
            out = unified_impact.query("Foo")

        impact.assert_called_once_with("repoA", transitive=False)
        peers.assert_called_once_with("repoA")
        self.assertEqual(out["resolved_repo"], "repoA")
        self.assertEqual(out["resolution"]["resolved_repo"], "repoA")
        self.assertEqual(out["dependency_edges"]["repo"], "repoA")
        self.assertEqual(out["dependency_edges"]["depended_on_by"], ["repoB"])
        self.assertEqual(out["message_edges"]["peers"], [{"peer_repo": "repoB"}])

    def test_symbol_collision_reports_all_defining_repos_without_changing_first_match_routing(self):
        hits = [
            os.path.join(config.MIRROR, "repoA", "src", "Foo.java") + ":3:class Foo {",
            os.path.join(config.MIRROR, "repoB", "src", "Foo.java") + ":4:class Foo {",
            os.path.join(config.MIRROR, "repoA", "other", "Foo.java") + ":5:class Foo {",
        ]
        with mock.patch.object(unified_impact, "_known_repos", return_value={"repoA", "repoB"}), \
             mock.patch.object(unified_impact.code, "search_code", return_value=hits), \
             mock.patch.object(unified_impact, "bundle_root_for", return_value=None), \
             mock.patch.object(unified_impact, "_call_graph", return_value={"available": False}), \
             mock.patch.object(
                 unified_impact.graph,
                 "impact",
                 return_value={"mode": "direct", "depended_on_by": [], "depends_on": []},
             ), \
             mock.patch.object(unified_impact, "_message_peers", return_value=[]):
            out = unified_impact.query("Foo")

        self.assertEqual(out["resolved_repo"], "repoA")
        self.assertEqual(out["routing_ambiguity"]["defining_repos"], ["repoA", "repoB"])
        self.assertEqual(out["routing_ambiguity"]["chosen"], "repoA")

    def test_repo_seed_is_used_directly_without_resolution(self):
        with mock.patch.object(unified_impact, "_known_repos", return_value={"repoA"}), \
             mock.patch.object(unified_impact, "bundle_root_for", return_value=None), \
             mock.patch.object(unified_impact, "_call_graph", return_value={"available": False}), \
             mock.patch.object(
                 unified_impact.graph, "impact",
                 return_value={"mode": "direct", "depended_on_by": [], "depends_on": []},
             ) as impact, \
             mock.patch.object(unified_impact, "_message_peers", return_value=[]):
            out = unified_impact.query("repoA")

        impact.assert_called_once_with("repoA", transitive=False)
        self.assertIsNone(out["resolved_repo"])
        self.assertNotIn("resolution", out)
        self.assertNotIn("routing_ambiguity", out)

    def test_unresolvable_symbol_is_left_as_is(self):
        with mock.patch.object(unified_impact, "_known_repos", return_value={"repoA"}), \
             mock.patch.object(unified_impact.code, "search_code", return_value=[]), \
             mock.patch.object(unified_impact, "bundle_root_for", return_value=None), \
             mock.patch.object(unified_impact, "_call_graph", return_value={"available": False}), \
             mock.patch.object(
                 unified_impact.graph, "impact",
                 return_value={"mode": "direct", "depended_on_by": [], "depends_on": []},
             ) as impact, \
             mock.patch.object(unified_impact, "_message_peers", return_value=[]):
            out = unified_impact.query("MysterySymbol")

        impact.assert_called_once_with("MysterySymbol", transitive=False)
        self.assertIsNone(out["resolved_repo"])


class CallGraphWrapperTests(unittest.TestCase):
    def test_public_call_graph_routes_then_explores(self):
        with mock.patch.object(unified_impact, "bundle_root_for", return_value="/root") as root, \
             mock.patch.object(
                 unified_impact, "_call_graph",
                 return_value={"available": True, "bundle_root": "/root"},
             ) as explore:
            out = unified_impact.call_graph("Foo")

        root.assert_called_once_with("Foo")
        explore.assert_called_once_with("Foo", cwd="/root")
        self.assertTrue(out["available"])


if __name__ == "__main__":
    unittest.main()

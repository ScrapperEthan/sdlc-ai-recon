"""The channel-evidence layer: shape tolerance, refusal to guess, and the three unknowns.

The assertions here are mostly NEGATIVE, and deliberately so. Every defect this layer could ship
has the same shape as the ones the project has already shipped: a thing that was not checked being
reported as if it had been. So the tests that matter are the ones that prove the engine says "I
cannot tell" when it cannot tell — an unscanned repo must not read as a channel-free repo, an
uncheckable citation must not read as a verified one, and a record missing its citation must be
dropped loudly rather than quietly kept.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retriever import channel_evidence  # noqa: E402


def _write(directory, name, payload):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class ContractTests(unittest.TestCase):
    def test_absent_contract_falls_back_to_builtin(self):
        payload = channel_evidence.contract("does-not-exist.json")
        self.assertEqual(payload["channels"], channel_evidence.DEFAULT_CONTRACT["channels"])

    def test_partial_override_keeps_the_other_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "contract.json", {"channels": ["sms"]})
            payload = channel_evidence.contract(path)
        self.assertEqual(payload["channels"], ["sms"])
        # A box overriding one key must not silently lose the field aliases and stop reading its
        # own file.
        self.assertIn("citation", payload["field_aliases"])

    def test_shipped_contract_matches_the_builtin_default(self):
        """The committed knob and DEFAULT_CONTRACT must not drift apart.

        DEFAULT_CONTRACT exists so an absent/corrupt config degrades to the same behaviour, which is
        only true while the two agree. A silent divergence would mean the box and this repo disagree
        about what a valid record is.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        shipped = channel_evidence.contract(
            os.path.join(root, "config", "channel_evidence.json"))
        for key in ("channels", "basis", "confidence", "relation_order"):
            self.assertEqual(shipped[key], channel_evidence.DEFAULT_CONTRACT[key], key)


class ShapeToleranceTests(unittest.TestCase):
    """Three shapes, one normalised result. The generator is the intranet's; a rename there must
    cost a config edit on their box, not a release from here."""

    def setUp(self):
        self.spec = channel_evidence._spec(channel_evidence.DEFAULT_CONTRACT)

    def test_v1_repo_keyed_single_citation(self):
        out = channel_evidence.normalise({
            "repo-a": {"channels": ["sms"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/src/Main.java:12"},
        }, self.spec)
        self.assertEqual(out["shape"], "v1")
        self.assertEqual(len(out["records"]["repo-a"]), 1)
        self.assertEqual(out["records"]["repo-a"][0]["line"], 12)

    def test_v1_multi_channel_shares_the_one_citation(self):
        out = channel_evidence.normalise({
            "repo-a": {"channels": ["sms", "email"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/src/Main.java:12"},
        }, self.spec)
        self.assertEqual({row["channel"] for row in out["records"]["repo-a"]}, {"sms", "email"})

    def test_v2_channel_keyed_many_citations(self):
        out = channel_evidence.normalise({
            "repo-a": {"channels": {
                "sms": [{"basis": "code", "confidence": "high", "citation": "repo-a/A.java:1"},
                        {"basis": "config", "confidence": "high", "citation": "repo-a/b.yml:2"}],
            }},
        }, self.spec)
        self.assertEqual(out["shape"], "v2")
        self.assertEqual(len(out["records"]["repo-a"]), 2)

    def test_flat_list_shape(self):
        out = channel_evidence.normalise([
            {"repo": "repo-a", "channel": "sms", "basis": "code", "confidence": "low",
             "citation": "repo-a/A.java:5"},
        ], self.spec)
        self.assertEqual(out["shape"], "list")
        self.assertEqual(out["records"]["repo-a"][0]["confidence"], "low")

    def test_field_aliases_are_case_and_separator_insensitive(self):
        spec = channel_evidence._spec(channel_evidence.contract("does-not-exist.json"))
        out = channel_evidence.normalise([
            {"Repo_Name": "repo-a", "Channel": "sms", "Evidence Type": "code",
             "Conf": "high", "Reference": "repo-a/A.java:9"},
        ], spec)
        self.assertEqual(out["records"]["repo-a"][0]["basis"], "code")


class RefusalTests(unittest.TestCase):
    """What must be thrown away, and counted while being thrown away."""

    def setUp(self):
        self.spec = channel_evidence._spec(channel_evidence.DEFAULT_CONTRACT)

    def test_missing_citation_is_dropped_and_counted(self):
        out = channel_evidence.normalise({
            "repo-a": {"channels": ["sms"], "basis": "code", "confidence": "high"},
        }, self.spec)
        self.assertEqual(out["records"], {})
        self.assertEqual(out["dropped"]["no_citation"], 1)

    def test_citation_without_a_line_number_is_dropped(self):
        out = channel_evidence.normalise({
            "repo-a": {"channels": ["sms"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/src/Main.java"},
        }, self.spec)
        self.assertEqual(out["dropped"]["malformed_citation"], 1)

    def test_zero_line_number_is_dropped(self):
        """`path:0` is a generator bug, not "no line" — accepting it lets an unverifiable
        citation through the one gate that is supposed to catch exactly that."""
        self.assertEqual(channel_evidence.parse_citation("repo-a/A.java:0"), (None, None))

    def test_channel_outside_the_enum_is_dropped_not_invented(self):
        out = channel_evidence.normalise({
            "repo-a": {"channels": ["carrier-pigeon"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/A.java:1"},
        }, self.spec)
        self.assertEqual(out["records"], {})
        self.assertEqual(out["dropped"]["unknown_channel"], 1)

    def test_unknown_confidence_defaults_DOWN_to_low(self):
        """Defaulting up would let a generator that forgets the field over-claim. Down can only
        ever under-claim, which is the safe direction for a notification list."""
        out = channel_evidence.normalise({
            "repo-a": {"channels": ["sms"], "basis": "code", "citation": "repo-a/A.java:1"},
        }, self.spec)
        self.assertEqual(out["records"]["repo-a"][0]["confidence"], "low")
        self.assertEqual(out["dropped"]["confidence_defaulted_low"], 1)

    def test_absent_file_is_reported_as_unreadable_not_as_empty(self):
        """"No file" and "a file that found nothing" need different remedies and must not look the
        same to a reader of the refresh report."""
        out = channel_evidence.load(path="does-not-exist.json")
        self.assertFalse(out["readable"])
        self.assertEqual(out["records"], {})

    def test_notes_are_withheld_by_default(self):
        """The note is generated by scanning source, so it can carry code fragments and vendor
        identifiers. The citation is the evidence; the note is commentary."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "ev.json", {
                "repo-a": {"channels": ["sms"], "basis": "code", "confidence": "high",
                           "citation": "repo-a/A.java:1", "note": "SENSITIVE"},
            })
            out = channel_evidence.load(path=path, contract_path="does-not-exist.json")
            self.assertEqual(out["records"]["repo-a"][0]["note"], "")
            opted_in = channel_evidence.load(path=path, contract_path="does-not-exist.json",
                                             include_notes=True)
            self.assertEqual(opted_in["records"]["repo-a"][0]["note"], "SENSITIVE")


class ScopeTests(unittest.TestCase):
    """"Found" and "looked at" are different facts, and the second one is the whole point."""

    def test_absent_scope_is_not_known(self):
        scope = channel_evidence.load_scope(path="does-not-exist.json")
        self.assertFalse(scope["known"])
        self.assertEqual(scope["scanned"], set())

    def test_unresolved_accepts_bare_names_and_reasoned_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "scope.json", {
                "scanned": ["repo-a", "repo-b"],
                "unresolved": ["repo-a", {"repo": "repo-b", "reason": "generated source"}],
            })
            scope = channel_evidence.load_scope(path=path, contract_path="does-not-exist.json")
        self.assertEqual(scope["scanned_count"], 2)
        self.assertEqual(scope["unresolved"]["repo-b"], "generated source")
        # A missing reason must not reject the entry: the name alone is still information.
        self.assertEqual(scope["unresolved"]["repo-a"], "")

    def test_reasons_are_carried_verbatim_and_grouped_never_interpreted(self):
        """The generator's reason string is theirs. I read RUNBOOK-77's "55 通过代码但无法保存引用"
        as "matched but uncitable" and shipped that meaning into a runbook and the UI; their reason
        string says the files were eligible and no channel marker was found, i.e. CLEAN. So the
        reason is grouped and quoted, never mapped onto a state of my choosing."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, "scope.json", {
                "scanned": ["a", "b", "c"],
                "unresolved": [{"repo": "a", "reason": "no_marker"},
                               {"repo": "b", "reason": "no_marker"},
                               {"repo": "c", "reason": "binary"}],
            })
            scope = channel_evidence.load_scope(path=path, contract_path="does-not-exist.json")
        self.assertEqual(scope["unresolved_reasons"], {"no_marker": 2, "binary": 1})

    def test_out_of_scope_population_is_reported_separately_from_the_bucket(self):
        """`unknown_unscanned` additionally requires that nothing else explains the repo, so it is a
        SUBSET of what was not scanned. The intranet caught that subset being quoted as the
        population (48 vs a real 83) — a number that understates what was never looked at while
        sounding like it reports it."""
        tags = {
            "scanned-and-tagged": {"channel": ["sms"], "channel_declared": [],
                                   "serves_channels": [], "msg_channels": []},
            "unscanned-but-named": {"channel": ["email"], "channel_declared": [],
                                    "serves_channels": [], "msg_channels": []},
            "unscanned-and-dark": {"channel": [], "channel_declared": [],
                                   "serves_channels": [], "msg_channels": []},
        }
        scope = {"known": True, "scanned": {"scanned-and-tagged"}, "scanned_count": 1,
                 "unresolved": {}, "unresolved_count": 0, "unresolved_reasons": {}}
        empty = {"readable": True, "records": {}, "repos": 0, "records_loaded": 0, "dropped": {},
                 "shape": "v1"}
        out = channel_evidence.coverage(tags=tags, evidence=empty, scope=scope)
        self.assertEqual(out["out_of_scope_total"], 2)   # two repos were never scanned
        self.assertEqual(out["unknown_unscanned"], 1)    # only one of them is otherwise unexplained
        self.assertEqual(out["scanned_total"], 1)


class CoverageTests(unittest.TestCase):
    """The three unknowns. Collapsing them is the failure this layer exists to prevent."""

    def _tags(self):
        return {
            "named": {"channel": ["sms"], "channel_declared": [], "serves_channels": [],
                      "msg_channels": []},
            "evidenced": {"channel": [], "channel_declared": [], "serves_channels": [],
                          "msg_channels": []},
            "propagated": {"channel": [], "channel_declared": [], "serves_channels": ["email"],
                           "msg_channels": []},
            "clean": {"channel": [], "channel_declared": [], "serves_channels": [],
                      "msg_channels": []},
            "never-looked-at": {"channel": [], "channel_declared": [], "serves_channels": [],
                                "msg_channels": []},
        }

    def _evidence(self):
        spec = channel_evidence._spec(channel_evidence.DEFAULT_CONTRACT)
        out = channel_evidence.normalise({
            "evidenced": {"channels": ["push"], "basis": "code", "confidence": "high",
                          "citation": "evidenced/A.java:3"},
        }, spec)
        return {"readable": True, "records": out["records"], "repos": 1, "records_loaded": 1,
                "dropped": {}, "shape": "v1"}

    def test_scanned_clean_and_out_of_scope_are_separate(self):
        scope = {"known": True, "scanned": {"named", "evidenced", "propagated", "clean"},
                 "scanned_count": 4, "unresolved": {}, "unresolved_count": 0}
        out = channel_evidence.coverage(tags=self._tags(), evidence=self._evidence(), scope=scope)
        self.assertEqual(out["has_direct"], 2)          # named + evidenced
        self.assertEqual(out["relation_only"], 1)       # propagated
        self.assertEqual(out["unknown_scanned"], 1)     # clean — a real finding
        self.assertEqual(out["unknown_unscanned"], 1)   # never-looked-at — NOT a finding
        self.assertEqual(out["repos_explained_by_evidence_alone"], 1)

    def test_without_a_scope_file_the_engine_refuses_to_choose(self):
        scope = {"known": False, "scanned": set(), "scanned_count": 0, "unresolved": {},
                 "unresolved_count": 0}
        out = channel_evidence.coverage(tags=self._tags(), evidence=self._evidence(), scope=scope)
        self.assertEqual(out["unknown_scope_unknown"], 2)
        self.assertEqual(out["unknown_scanned"], 0)
        self.assertEqual(out["unknown_unscanned"], 0)


class RelationTests(unittest.TestCase):
    def _evidence(self, payload):
        spec = channel_evidence._spec(channel_evidence.DEFAULT_CONTRACT)
        out = channel_evidence.normalise(payload, spec)
        return {"readable": True, "records": out["records"], "repos": len(out["records"]),
                "records_loaded": sum(len(v) for v in out["records"].values()),
                "dropped": out["dropped"], "shape": out["shape"]}

    def test_one_entry_per_channel_with_every_reason_kept(self):
        """Four rows all saying SMS is one channel with four reasons, not four channels."""
        tags = {"repo-a": {"channel": ["sms"], "channel_declared": ["sms"], "msg_channels": ["sms"],
                           "serves_channels": []}}
        evidence = self._evidence({
            "repo-a": {"channels": ["sms"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/A.java:1"},
        })
        view = channel_evidence.for_repo("repo-a", tags=tags, evidence=evidence,
                                         contract_path="does-not-exist.json")
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["relation"], "direct_code_evidence")
        self.assertEqual(len(view[0]["relations"]), 4)

    def test_transitive_is_never_ownership(self):
        tags = {"repo-a": {"channel": [], "channel_declared": [], "msg_channels": [],
                           "serves_channels": ["sms"]}}
        view = channel_evidence.for_repo("repo-a", tags=tags, evidence=self._evidence({}),
                                         contract_path="does-not-exist.json")
        self.assertEqual(view[0]["relation"], "transitive_dependency")
        self.assertFalse(view[0]["direct"])
        split = channel_evidence.split_channels(view)
        self.assertEqual(split["direct_channels"], [])
        self.assertEqual(split["affected_channels"], ["sms"])

    def test_low_confidence_code_ranks_below_the_message_graph(self):
        """A channel word in a class name is a worse reason than a topic that demonstrably carries
        the channel. Ranking it above would let the weakest evidence lead the answer."""
        tags = {"repo-a": {"channel": [], "channel_declared": [], "msg_channels": ["sms"],
                           "serves_channels": []}}
        evidence = self._evidence({
            "repo-a": {"channels": ["sms"], "basis": "code", "confidence": "low",
                       "citation": "repo-a/A.java:1"},
        })
        view = channel_evidence.for_repo("repo-a", tags=tags, evidence=evidence,
                                         contract_path="does-not-exist.json")
        self.assertEqual(view[0]["relations"][0]["relation"], "message_carried")


class ConflictTests(unittest.TestCase):
    def _evidence(self, payload):
        spec = channel_evidence._spec(channel_evidence.DEFAULT_CONTRACT)
        out = channel_evidence.normalise(payload, spec)
        return {"readable": True, "records": out["records"], "repos": len(out["records"]),
                "records_loaded": 1, "dropped": {}, "shape": "v1"}

    def test_disjoint_channels_are_reported_not_merged(self):
        """The likeliest reading is that the name is historical and the code is current — but that
        is the repo owner's call, and it can only be made if the disagreement is visible."""
        tags = {"repo-a": {"channel": ["sms"], "channel_declared": [], "msg_channels": [],
                           "serves_channels": []}}
        evidence = self._evidence({
            "repo-a": {"channels": ["email"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/A.java:1"},
        })
        rows = channel_evidence.conflicts(tags=tags, evidence=evidence,
                                          contract_path="does-not-exist.json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["structural_channels"], ["sms"])
        self.assertEqual(rows[0]["evidenced_channels"], ["email"])

    def test_overlapping_channels_are_enrichment_not_conflict(self):
        tags = {"repo-a": {"channel": ["sms"], "channel_declared": [], "msg_channels": [],
                           "serves_channels": []}}
        evidence = self._evidence({
            "repo-a": {"channels": ["sms", "email"], "basis": "code", "confidence": "high",
                       "citation": "repo-a/A.java:1"},
        })
        self.assertEqual(channel_evidence.conflicts(tags=tags, evidence=evidence,
                                                    contract_path="does-not-exist.json"), [])


class CitationExtensionTests(unittest.TestCase):
    """Extensions the intranet's real run cited and the verifier could not check.

    A missing extension does NOT fail loudly — it yields no match, so the reference never enters
    the guard and the report says "0 citations" rather than "1 unverified". Under-inclusion is
    therefore the dangerous direction, and it was silently exempting Groovy build scripts and
    portal JS from the citation check entirely.
    """

    def test_groovy_and_js_are_extracted(self):
        from retriever import citations
        for ref in ("repo-a/build.groovy:12", "repo-a/static/app.js:7",
                    "repo-a/src/main.ts:3", "repo-a/src/App.tsx:1"):
            self.assertEqual([item[0] for item in citations.extract(ref)], [ref], ref)

    def test_json_still_wins_over_the_js_prefix(self):
        """Alternation is ordered and unanchored, so `jsx?` placed before `json` would capture
        `package.js` out of `package.json` — turning a real citation into a missing file. The
        trailing lookahead is what prevents it."""
        from retriever import citations
        found = citations.extract("repo-a/package.json:4")
        self.assertEqual([item[0] for item in found], ["repo-a/package.json:4"])
        self.assertEqual(found[0][1], "repo-a/package.json")


class VerificationTests(unittest.TestCase):
    def test_an_unrecognised_extension_is_unverifiable_never_verified(self):
        """`citations.verify` extracts by known extension, so an unrecognised one yields zero items.
        Reading "nothing was checked" as "everything passed" is the exact shape this layer refuses.
        """
        loaded = {"records": {"repo-a": [{
            "repo": "repo-a", "channel": "sms", "basis": "code", "confidence": "high",
            "citation": "repo-a/some/file.weirdext:4", "path": "repo-a/some/file.weirdext",
            "line": 4, "note": "",
        }]}}
        out = channel_evidence.verify(loaded)
        self.assertEqual(out["unverifiable"], 1)
        self.assertEqual(out["ok"], 0)


if __name__ == "__main__":
    unittest.main()

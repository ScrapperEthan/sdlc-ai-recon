"""RUNBOOK-49: 3HK SMSC over-count + htcl/3hk vendor split.

Two guards:
  1. make_delivery_topology folds 3HK's "htcl" repo token onto the canonical "3hk" bucket, so
     the diagram's 3HK nodes (vendor="3hk") bind the real 3HK repos instead of an empty set, and
     the vendor-scoped external node stops swallowing CSL/CM/Sinch (the reported bug).
  2. The real static/arch_nodes.json never ships a vendor-less external node on a channel that has
     other vendors — that is exactly the defect that made 3HK SMSC bind all 69 SMS repos.
"""
import csv
import json
import os
import tempfile
import unittest

import make_arch_map
import make_delivery_topology

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH_NODES = os.path.join(ROOT, "static", "arch_nodes.json")

# Real-shaped SMS repos: 3HK under its legal token "htcl", plus two other SMS vendors that must
# stay in their own buckets (the ones the colleague saw wrongly counted under 3HK SMSC).
REPOS = [
    "amet-mdc-hsbc-svc-rt-hr-htcl-sms-deli-job",  # 3HK delivery job, tokened htcl
    "mc-hk-hase-htcl-outbound-api",               # 3HK outbound API, tokened htcl (Bug 1 repo)
    "amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job",   # CSL — must NOT land under 3hk
    "amet-mdc-hsbc-svc-rt-hr-cm-sms-deli-job",    # CM delivery job — must NOT land under 3hk
    "amet-mdc-hsbc-cm-outbound-api",              # CM outbound API — buckets to cm, not 3hk
]


def _topology(tmp):
    edges = os.path.join(tmp, "internal_edges.csv")
    with open(edges, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["from_repo", "to_repo"])
        for repo in REPOS:
            writer.writerow([repo, "api-starter"])
    payload, _, _ = make_delivery_topology.build_topology(
        edges_path=edges,
        override_path=os.path.join(tmp, "missing_override.json"),
        repo_tags_path=os.path.join(tmp, "missing_tags.json"),
    )
    return payload


class VendorAliasTests(unittest.TestCase):
    def test_htcl_repos_bucket_under_canonical_3hk(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = _topology(tmp)
        sms = payload["sms"]
        self.assertIn("3hk", sms)
        self.assertNotIn("htcl", sms, "htcl must be folded into the canonical 3hk bucket")
        deli = {job["repo"] for job in sms["3hk"]["delivery_jobs"]}
        apis = {api["repo"] for api in sms["3hk"]["outbound_apis"]}
        self.assertIn("amet-mdc-hsbc-svc-rt-hr-htcl-sms-deli-job", deli)
        self.assertIn("mc-hk-hase-htcl-outbound-api", apis)
        # The other vendors keep their own buckets — never absorbed into 3hk.
        self.assertIn("csl", sms)
        self.assertIn("cm", sms)

    def test_canon_vendor_is_identity_for_unknown(self):
        self.assertEqual(make_delivery_topology.canon_vendor("csl"), "csl")
        self.assertEqual(make_delivery_topology.canon_vendor("htcl"), "3hk")

    def test_2way_qualifier_does_not_become_the_vendor(self):
        """`htcl-2way-sms` must bucket under 3hk (via htcl), not a phantom `2way` vendor —
        2-way SMS is a 3HK flow (owner-confirmed 2026-07-23)."""
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            with open(edges, "w", newline="", encoding="utf-8") as handle:
                w = csv.writer(handle)
                w.writerow(["from_repo", "to_repo"])
                w.writerow(["mc-hk-hase-svc-bat-htcl-2way-sms-deli-job", "api-starter"])
                w.writerow(["amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job", "api-starter"])
            payload, _, _ = make_delivery_topology.build_topology(
                edges_path=edges,
                override_path=os.path.join(tmp, "no_override.json"),
                repo_tags_path=os.path.join(tmp, "no_tags.json"),
            )
        sms = payload["sms"]
        self.assertNotIn("2way", sms, "'2way' is a message-type qualifier, not a vendor")
        self.assertIn("mc-hk-hase-svc-bat-htcl-2way-sms-deli-job",
                      {job["repo"] for job in sms["3hk"]["delivery_jobs"]})
        # the qualifier is preserved as message_type for later 2-way queries
        job = next(j for j in sms["3hk"]["delivery_jobs"] if "2way" in j["repo"])
        self.assertEqual(job.get("message_type"), "2way")

    def test_3hk_external_node_binds_only_3hk_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            topology = _topology(tmp)
        catalog = {"nodes": [
            {"id": "ext-3hk-smsc", "label": "3HK SMSC", "role": "external", "kind": "vendor",
             "channel": "sms", "vendor": "3hk"},
        ]}
        nodes = make_arch_map.build_map(catalog, topology, {})
        repos = set(nodes["ext-3hk-smsc"]["repos"])
        self.assertEqual(repos, {
            "amet-mdc-hsbc-svc-rt-hr-htcl-sms-deli-job",
            "mc-hk-hase-htcl-outbound-api",
        })
        # The reported bug: CSL/CM repos must no longer show under 3HK SMSC.
        self.assertNotIn("amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job", repos)
        self.assertNotIn("amet-mdc-hsbc-cm-outbound-api", repos)


class VendorTokenPickingTests(unittest.TestCase):
    """RUNBOOK-51: the vendor is the rightmost KNOWN carrier token, so mode/system words and split
    ICCM variants stop becoming phantom SMS vendor buckets, and a vendor-less generic delivery job
    no longer creates a `hase` bucket that steals outbound APIs."""

    def _topo(self, repos):
        with tempfile.TemporaryDirectory() as tmp:
            edges = os.path.join(tmp, "internal_edges.csv")
            with open(edges, "w", newline="", encoding="utf-8") as handle:
                w = csv.writer(handle)
                w.writerow(["from_repo", "to_repo"])
                for r in repos:
                    w.writerow([r, "api-starter"])
            payload, _, _ = make_delivery_topology.build_topology(
                edges_path=edges,
                override_path=os.path.join(tmp, "no_override.json"),
                repo_tags_path=os.path.join(tmp, "no_tags.json"),
            )
        return payload

    def test_mode_and_system_tokens_are_not_vendors(self):
        topo = self._topo([
            "mc-hk-hase-sms-deli-job",                  # no carrier token → unknown
            "mc-hk-hase-svc-rt-hr-csl-sms-deli-job",    # hr is a mode word → carrier is csl
        ])
        sms = topo["sms"]
        self.assertNotIn("hase", sms)
        self.assertNotIn("hr", sms)
        self.assertIn("csl", sms)
        self.assertIn(make_delivery_topology.UNKNOWN_VENDOR, sms)
        self.assertIn("mc-hk-hase-sms-deli-job",
                      {j["repo"] for j in sms[make_delivery_topology.UNKNOWN_VENDOR]["delivery_jobs"]})

    def test_iccm_family_folds_to_iccm(self):
        topo = self._topo([
            "mc-hk-hsbc-svc-bat-iccms-sms-deli-job",
            "mc-hk-hase-svc-tc-iccmpt-sms-deli-job",
        ])
        sms = topo["sms"]
        self.assertIn("iccm", sms)
        for phantom in ("iccms", "iccmpt", "iccmh", "iccmt", "iccmv"):
            self.assertNotIn(phantom, sms)
        self.assertEqual(len(sms["iccm"]["delivery_jobs"]), 2)

    def test_generic_deli_job_does_not_steal_outbound_apis(self):
        # the `mc-hk-hase-sms-deli-job` → "hase" regression: with no "hase" vendor bucket, the
        # mc-hk-hase outbound API is not grabbed into it; it resolves to its real carrier (3hk).
        topo = self._topo([
            "mc-hk-hase-sms-deli-job",
            "mc-hk-hase-svc-bat-htcl-sms-deli-job",   # gives known_vendors a real 3hk
            "mc-hk-hase-htcl-outbound-api",
        ])
        sms = topo["sms"]
        self.assertNotIn("hase", sms)
        self.assertIn("mc-hk-hase-htcl-outbound-api",
                      {a["repo"] for a in sms["3hk"]["outbound_apis"]})


class ArchCatalogInvariantTests(unittest.TestCase):
    def test_no_vendorless_external_node_on_a_multivendor_channel(self):
        """A vendor-less external node binds EVERY vendor on its channel (see test_arch_map). That is
        only safe when it is the sole vendor node for that channel; otherwise it swallows its
        siblings' repos — the 3HK SMSC / 69-repo bug. Guard the shipped catalog against a repeat."""
        with open(ARCH_NODES, encoding="utf-8-sig") as handle:
            catalog = json.load(handle)
        vendor_nodes = [
            node for node in catalog["nodes"]
            if (node.get("role") == "external" and node.get("kind") == "vendor" and node.get("channel"))
        ]
        by_channel = {}
        for node in vendor_nodes:
            by_channel.setdefault(node["channel"], []).append(node)
        offenders = []
        for channel, group in by_channel.items():
            if len(group) > 1:
                offenders += [
                    node["id"] for node in group if not (node.get("vendor") or "").strip()
                ]
        self.assertEqual(
            offenders, [],
            f"vendor-less external node(s) on a multi-vendor channel bind all vendors: {offenders}",
        )

    def test_aurora_push_lane_present_and_scoped(self):
        """Aurora is an explicit push provider (own HTTPS client/cert), not generic SNS→APNs/FCM.
        The catalog must carry a vendor-scoped Aurora outbound + terminal, wired off push-deli."""
        with open(ARCH_NODES, encoding="utf-8-sig") as handle:
            catalog = json.load(handle)
        nodes = {n["id"]: n for n in catalog["nodes"]}
        self.assertEqual(nodes["ext-aurora"].get("vendor"), "aurora")
        self.assertEqual(nodes["push-aurora"].get("vendor"), "aurora")
        # APNs/FCM must now be vendor-scoped too, else it swallows the Aurora repos again.
        self.assertEqual(nodes["ext-apns-fcm"].get("vendor"), "sns")
        edges = {tuple(e[:2]) for e in catalog["edges"]}
        self.assertIn(("push-deli", "push-aurora"), edges)
        self.assertIn(("push-aurora", "ext-aurora"), edges)


if __name__ == "__main__":
    unittest.main()

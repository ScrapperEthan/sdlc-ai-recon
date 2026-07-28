"""Every committed file must have a declared owner (OWNERSHIP.json).

AGENTS.md describes the intranet/external boundary in prose, which is readable but rots: files get
added, nobody updates the table, and a few months later the boundary is decorative. This test makes
the declaration load-bearing — add a file without saying who owns it and the suite goes red, so the
ledger cannot silently drift out of date.

It is a governance check, not a permissions system: nothing stops an edit, it only stops the map
from going stale.
"""
import fnmatch
import json
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNERSHIP = os.path.join(ROOT, "OWNERSHIP.json")


def _tracked_files():
    """git ls-files, so gitignored artefacts (index/, webapp_data/, mirror/) are naturally out."""
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line.strip().replace("\\", "/") for line in out.stdout.splitlines() if line.strip()]


def _owner_for(path, rules):
    for rule in rules:
        pattern = rule["pattern"]
        if fnmatch.fnmatch(path, pattern):
            return rule["owner"]
        # fnmatch's '*' crosses '/', so 'dir/**' already covers nested paths, but a bare 'dir/*'
        # rule should still claim only its own level — that is the fnmatch default here.
    return None


class OwnershipLedgerTests(unittest.TestCase):
    def setUp(self):
        with open(OWNERSHIP, encoding="utf-8") as handle:
            self.ledger = json.load(handle)
        self.rules = self.ledger["rules"]

    def test_every_declared_owner_is_defined(self):
        known = set(self.ledger["owners"])
        for rule in self.rules:
            self.assertIn(rule["owner"], known, f"rule {rule['pattern']} has an unknown owner")

    def test_every_tracked_file_has_an_owner(self):
        unowned = [path for path in _tracked_files() if _owner_for(path, self.rules) is None]
        self.assertEqual(unowned, [], (
            "These committed files have no owner in OWNERSHIP.json. Add a rule saying who maintains "
            "them — the test asks 'what event would make this file need to change?': data/schema/"
            "environment => intranet, a new assistant capability => external, record-keeping => "
            "shared."))

    def test_the_knob_dir_stays_intranet_owned(self):
        """config/ is the seam that lets the intranet side ship a data change without an engine
        edit (AGENTS.md 4). If a rule ever reassigns it, that seam is gone."""
        for name in ("config/alarm_patterns.json", "config/source_system_aliases.json",
                     "config/rule_text_semantics.json"):
            self.assertEqual(_owner_for(name, self.rules), "intranet", name)

    def test_the_engine_stays_external_owned(self):
        for name in ("webapp/agent.py", "webapp/context_budget.py", "retriever/incident.py",
                     "prompts/qa-system-prompt.md"):
            self.assertEqual(_owner_for(name, self.rules), "external", name)


if __name__ == "__main__":
    unittest.main()

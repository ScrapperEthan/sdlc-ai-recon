"""The incident sub-agent is four modules now; these are the properties that keep it four.

`incident_investigator` was 2846 lines doing planning, parsing, orchestration and redaction at once.
Splitting it only stays worth anything if the direction of the dependencies holds, and nothing in
the modules themselves makes that visible — an `import` added in the wrong direction looks exactly
like one added in the right one.

The layering, strictly one-way:

    redaction        <- nothing (leaf; the shared exit gate)
    incident_parse   <- redaction                     "what did they actually say?"
    incident_plan    <- retriever                     "what should we ask?"
    incident_investigator <- all three                orchestration + the exit

Two consequences are load-bearing rather than tidy:

* `incident_parse` must NOT import the retrieval stack. That is precisely what lets `mcp_console`
  import it at module scope for the shape report; while it lived inside `incident_investigator` the
  console had to import it lazily inside a function to avoid dragging the whole mirror in.
* nothing under `incident_investigator` may import it back. A cycle here would work by luck of
  import order and then break on the day someone imports the modules in the other order.
"""
import ast
import os
import unittest

WEBAPP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")


def _imports(module):
    """Every module name `module` imports, as written (`retriever.code`, `.redaction` -> its name)."""
    with open(os.path.join(WEBAPP, module + ".py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # from . import x  /  from .redaction import y
                found |= {node.module} if node.module else {a.name for a in node.names}
            else:
                found.add(node.module)
    return {name for name in found if name}


class LayerDirectionTests(unittest.TestCase):

    def test_redaction_is_a_leaf(self):
        """The exit gate depends on nothing of ours, so no import cycle can ever route around it."""
        self.assertEqual({i for i in _imports("redaction") if not i.startswith(("re", "hashlib"))},
                         set())

    def test_nothing_under_the_investigator_imports_it_back(self):
        for module in ("redaction", "incident_parse", "incident_plan"):
            with self.subTest(module=module):
                self.assertNotIn("incident_investigator", _imports(module))

    def test_the_parse_layer_does_not_drag_in_the_retrieval_stack(self):
        """This is why `mcp_console` can import it at module scope. Adding a `retriever` import here
        would silently push that back to a lazy in-function import, or make the console depend on a
        built mirror it has no use for."""
        retrieval = {name for name in _imports("incident_parse") if name.startswith("retriever")}
        self.assertEqual(retrieval, set())

    def test_the_console_reaches_the_parse_layer_directly(self):
        """Not through `incident_investigator` — the whole point of the previous test."""
        imports = _imports("mcp_console")
        self.assertIn("incident_parse", imports)
        self.assertNotIn("incident_investigator", imports)


class OneBindingPerNameTests(unittest.TestCase):
    """A moved name must have exactly one binding, in the module that owns it.

    `from incident_plan import log_sources` in the investigator would create a second binding, and a
    test patching `incident_plan.log_sources` would then be invisible to the investigator's calls —
    the test passes, the real function runs. That is the worst kind of green, so the investigator
    calls everything module-qualified and imports only values.
    """

    FUNCTIONS = {
        "incident_plan": ["plan", "log_sources", "app_candidates", "_app_map", "exception_classes",
                          "alert_time_format", "to_utc", "metric_window_bounds"],
        "incident_parse": ["response_shape", "_decode", "extract_log_lines", "extract_app_names",
                           "select_log_files", "alarm_metric_identity", "parse_metric_window",
                           "_tool_outcome", "describe_shape", "describe_response"],
    }

    @staticmethod
    def _from_imported(consumer, owner):
        with open(os.path.join(WEBAPP, consumer + ".py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == owner:
                names |= {alias.name for alias in node.names}
        return names

    def test_the_investigator_does_not_from_import_a_moved_function(self):
        for owner, functions in self.FUNCTIONS.items():
            imported = self._from_imported("incident_investigator", owner)
            for name in functions:
                with self.subTest(owner=owner, name=name):
                    self.assertNotIn(
                        name, imported,
                        f"{name} is from-imported; call it as {owner}.{name} so a patch on the "
                        f"owner is seen here too")

    def test_what_the_investigator_does_import_is_values_not_behaviour(self):
        """Constants are fine to bind twice — nothing patches them, and they read as this module's
        own vocabulary. The rule is only about things that can be replaced."""
        for owner in self.FUNCTIONS:
            for name in self._from_imported("incident_investigator", owner):
                with self.subTest(owner=owner, name=name):
                    self.assertTrue(name.isupper() or name.lstrip("_").isupper()
                                    or name.upper() == name,
                                    f"{name} looks like a function, not a constant")


if __name__ == "__main__":
    unittest.main()

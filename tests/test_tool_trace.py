"""The clickable tool trace: one ledger entry per call, and the model told what actually failed.

Two defects are covered here, and they are the same defect in two places — a failure recorded as
having happened without recording what it was:

* the browser showed a row of chips carrying the tool NAME only, so a call that answered, a call
  rejected for a mistyped argument and a call that never ran all looked identical;
* the model was handed `{"error": "'repo'"}` — a bare `str(KeyError)` — or, when its tool-call JSON
  was malformed, `args = {}` and then a MISSING FIELD complaint about a field that was never the
  problem. It would resend the same field, correctly, for as many rounds as it had.

The tests that matter most are the attribution ones: an unclassified failure must land on
`internal_error` and never on `bad_arguments` (or the model is sent to fix an argument nobody showed
was wrong), and `who_can_close` for our own error must never point at the user.
"""
import contextlib
import json
import os
import re
import unittest
from unittest import mock

from webapp import agent, config, tool_trace, tools

STATIC = os.path.join(os.path.dirname(os.path.abspath(agent.__file__)), "static")


def _call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _run(rounds, dispatch=None, **config_overrides):
    """One agent turn: `rounds` is a list of tool-call batches, then the model answers.

    Returns the event list. A batch is a list of tool calls exactly as a provider would send them.
    """
    replies = [{"role": "assistant", "content": None, "tool_calls": batch} for batch in rounds]
    replies.append({"role": "assistant", "content": "done"})
    remaining = iter(replies)

    def _chat(messages, tool_list=None):
        yield ("final", next(remaining))

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(agent.llm, "chat_stream", _chat))
        stack.enter_context(mock.patch.object(agent.llm, "stream_text", lambda m: []))
        if dispatch is not None:
            stack.enter_context(mock.patch.object(tools, "dispatch", dispatch))
        for key, value in config_overrides.items():
            stack.enter_context(mock.patch.object(config, key, value))
        return list(agent.answer_events("q"))


def _trace(events):
    for event in events:
        if event.get("type") == "done":
            return event.get("tool_trace") or []
    raise AssertionError("no done event")


def _tool_message(entry):
    """What the model was handed for this call — the trace keeps the exact string."""
    return json.loads(entry["output"]["text"])


class BadArgumentsTests(unittest.TestCase):

    def test_a_missing_required_argument_is_named_and_the_tool_never_runs(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("impact", "{}")]], dispatch=dispatch))[0]
        dispatch.assert_not_called()
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["failure_class"], "bad_arguments")
        self.assertEqual([item["field"] for item in entry["invalid"]], ["repo"])
        self.assertEqual(entry["invalid"][0]["problem"], "missing")
        self.assertEqual(entry["invalid"][0]["expected"], "string")
        self.assertEqual(entry["invalid"][0]["actual_type"], "absent")

    def test_the_model_is_told_the_field_and_gets_another_round(self):
        """The self-repair loop: a rejected proposal is an observation, not the end of the turn."""
        dispatch = mock.Mock(return_value={"ok": True, "hubs": []})
        events = _run([[_call("impact", "{}")], [_call("impact", '{"repo": "amet-mdc-sms"}')]],
                      dispatch=dispatch)
        trace = _trace(events)
        self.assertEqual([item["status"] for item in trace], ["error", "ok"])
        told = _tool_message(trace[0])
        self.assertEqual(told["failure_class"], "bad_arguments")
        self.assertEqual(told["invalid_arguments"][0]["field"], "repo")
        self.assertEqual(told["who_can_close"], "assistant")
        dispatch.assert_called_once_with("impact", {"repo": "amet-mdc-sms"})

    def test_an_empty_required_string_is_rejected_rather_than_looked_up(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("impact", '{"repo": "  "}')]], dispatch=dispatch))[0]
        dispatch.assert_not_called()
        self.assertEqual(entry["invalid"][0]["problem"], "empty")
        self.assertEqual(entry["invalid"][0]["actual_type"], "str(empty)")

    def test_a_container_where_a_name_belongs_is_stopped_and_named(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("impact", '{"repo": {"name": "x"}}')]], dispatch=dispatch))[0]
        dispatch.assert_not_called()
        self.assertEqual(entry["invalid"][0]["problem"], "wrong_type")
        self.assertEqual(entry["invalid"][0]["actual_type"], "object(keys=1)")

    def test_an_unknown_tool_name_is_the_callers_to_fix_not_ours(self):
        """Refused by `dispatch`, which declares the class — NOT by the schema check.

        The ten pre-consolidation names still route there for the CLI and MCP callers, so a name
        missing from `TOOLS` cannot be refused up front without breaking a path that works today.
        What matters is the attribution: a name the model invented must not be reported to it as
        our own internal error, which it can only answer by giving up.
        """
        entry = _trace(_run([[_call("no_such_tool", "{}")]]))[0]
        self.assertEqual(entry["failure_class"], "bad_arguments")
        self.assertEqual(entry["message"], "unknown tool: no_such_tool")
        self.assertEqual(entry["notes"][0]["problem"], "no_schema")

    def test_a_legacy_name_still_reaches_its_handler(self):
        """`show_impact` and nine others left TOOLS but not dispatch."""
        entry = _trace(_run([[_call("consumers", '{"destination": "topic-x"}')]],
                            dispatch=lambda n, a: {"ok": True, "matches": []}))[0]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["notes"][0]["problem"], "no_schema")

    def test_the_argument_values_themselves_never_reach_the_model(self):
        """`actual_type` is a shape. An argument can carry a pasted alert or a customer id."""
        entry = _trace(_run([[_call("show_arch", '{"kind": "vendor"}')]]))[0]
        told = json.dumps(_tool_message(entry))
        self.assertIn("value", told)          # the field NAME is public: it is in the schema
        self.assertNotIn("vendor", told)      # the value it was called with is not


class LooseArgumentsAreRecordedNotRejectedTests(unittest.TestCase):
    """The no-regression half. A `"50"` where an integer belongs is what several tools already do
    `int(...)` on — tightening that would break calls that work today, which is the one thing this
    change may not do. So it runs unchanged, and the panel says what happened."""

    def test_a_numeric_string_still_dispatches_and_is_noted(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("hubs", '{"top": "5"}')]], dispatch=dispatch))[0]
        dispatch.assert_called_once_with("hubs", {"top": "5"})
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["invalid"], [])
        self.assertEqual(entry["notes"][0], {"field": "top", "problem": "loose_type",
                                             "expected": "integer", "actual_type": "str(len=1)"})

    def test_an_unknown_argument_is_noted_and_the_call_still_runs(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("hubs", '{"topp": 5}')]], dispatch=dispatch))[0]
        dispatch.assert_called_once()
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["notes"][0]["problem"], "unknown_field")
        self.assertIn("top", entry["notes"][0]["expected"])


class BadCallSyntaxTests(unittest.TestCase):
    """Malformed arguments used to become `{}` and get dispatched, so the model was told a field was
    missing when its JSON was the fault. It then resent the same field — a deadlock costing the turn.
    """

    def test_unparsable_arguments_are_their_own_class_and_name_no_field(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("impact", '{"repo": "amet-mdc-')]], dispatch=dispatch))[0]
        dispatch.assert_not_called()
        self.assertEqual(entry["failure_class"], "bad_call_syntax")
        self.assertEqual(entry["invalid"], [])
        told = _tool_message(entry)
        self.assertNotIn("invalid_arguments", told)
        self.assertIn("JSON", told["guidance"])

    def test_the_raw_arguments_are_kept_for_the_human_panel(self):
        entry = _trace(_run([[_call("impact", '{"repo": "amet-mdc-')]]))[0]
        self.assertEqual(entry["arguments_raw"], '{"repo": "amet-mdc-')

    def test_a_json_array_of_arguments_is_a_syntax_failure_not_a_missing_field(self):
        entry = _trace(_run([[_call("impact", '["amet-mdc-sms"]')]]))[0]
        self.assertEqual(entry["failure_class"], "bad_call_syntax")

    def test_absent_arguments_are_legitimate_for_a_tool_that_requires_none(self):
        dispatch = mock.Mock(return_value={"ok": True})
        entry = _trace(_run([[_call("hubs", "")]], dispatch=dispatch))[0]
        dispatch.assert_called_once_with("hubs", {})
        self.assertEqual(entry["status"], "ok")


class AttributionTests(unittest.TestCase):
    """Who gets sent to fix it. Sending a user to close a gap they cannot close is worse than
    telling them nothing, and sending the model to fix an argument that was fine deadlocks the run.
    """

    def test_an_exception_is_ours_and_says_so_without_a_stack(self):
        def _boom(name, args):
            raise KeyError("I0141x")

        entry = _trace(_run([[_call("usecase_impact", '{"use_case_id": "I0141x"}')]],
                            dispatch=_boom))[0]
        self.assertEqual(entry["failure_class"], "internal_error")
        self.assertEqual(entry["who_can_close"], "us")
        told = _tool_message(entry)
        self.assertEqual(told["error_type"], "KeyError")
        self.assertNotIn("Traceback", json.dumps(told))
        self.assertIn("Do NOT ask", told["guidance"])

    def test_an_exception_does_not_end_the_turn(self):
        """The two lines that make this Ask repair itself: the error becomes a tool result."""
        calls = iter([Exception("nope"), {"ok": True, "rows": []}])

        def _flaky(name, args):
            outcome = next(calls)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        trace = _trace(_run([[_call("hubs", "{}")], [_call("hubs", "{}")]], dispatch=_flaky))
        self.assertEqual([item["status"] for item in trace], ["error", "ok"])

    def test_an_undeclared_tool_failure_is_never_blamed_on_the_arguments(self):
        entry = _trace(_run([[_call("hubs", "{}")]],
                            dispatch=lambda n, a: {"ok": False, "error": "index missing"}))[0]
        self.assertEqual(entry["failure_class"], "internal_error")
        self.assertEqual(entry["message"], "index missing")
        self.assertTrue(entry["dispatched"])

    def test_a_rejected_call_records_that_nothing_ran(self):
        """"Never ran" and "ran and failed" are different facts, and the panel says which."""
        rejected = _trace(_run([[_call("impact", "{}")]]))[0]
        self.assertFalse(rejected["dispatched"])
        self.assertEqual(rejected["message"], "")   # no tool to quote; no invented sentence

    def test_a_tool_may_declare_its_own_class(self):
        entry = _trace(_run([[_call("hubs", "{}")]],
                            dispatch=lambda n, a: {"ok": False, "failure_class": "refused",
                                                   "error": "caller_policy"}))[0]
        self.assertEqual(entry["failure_class"], "refused")
        self.assertEqual(entry["who_can_close"], "config_owner")

    def test_a_class_we_do_not_recognise_is_not_trusted(self):
        entry = _trace(_run([[_call("hubs", "{}")]],
                            dispatch=lambda n, a: {"ok": False, "failure_class": "totally_fine"}))[0]
        self.assertEqual(entry["failure_class"], "internal_error")

    def test_a_pii_shaped_exception_message_is_masked_before_the_model_sees_it(self):
        def _leak(name, args):
            raise ValueError("no route for someone@example.com")

        entry = _trace(_run([[_call("hubs", "{}")]], dispatch=_leak))[0]
        self.assertNotIn("someone@example.com", json.dumps(_tool_message(entry)))

    def test_no_failure_class_ever_sends_the_user_to_fix_our_wiring(self):
        self.assertEqual(tool_trace.WHO_CAN_CLOSE["internal_error"], "us")
        self.assertNotIn("user", set(tool_trace.WHO_CAN_CLOSE.values()))

    def test_every_class_declared_in_the_tool_layer_is_in_the_closed_enum(self):
        """The enum lives in one module; the tools declare into it. This is the drift guard."""
        source = open(tools.__file__, encoding="utf-8").read()
        declared = set(re.findall(r'"failure_class":\s*"([a-z_]+)"', source))
        self.assertTrue(declared, "expected the tool layer to declare at least one class")
        self.assertLessEqual(declared, tool_trace.FAILURE_CLASSES)


class OneLedgerTests(unittest.TestCase):
    """The panel and the conversation read the same entry. Two accounts of one call is how a screen
    ends up saying 'Harness did not approve' beside a peer's refused connection."""

    def test_the_recorded_output_is_exactly_what_the_model_was_handed(self):
        result = {"ok": True, "items": ["a", "b"]}
        entry = _trace(_run([[_call("hubs", "{}")]], dispatch=lambda n, a: result))[0]
        self.assertEqual(_tool_message(entry), result)
        self.assertEqual(entry["output"]["model_chars"], entry["output"]["shown_chars"])
        self.assertEqual(entry["output"]["result_chars"],
                         len(json.dumps(result, ensure_ascii=False)))

    def test_a_shrunk_result_states_both_sizes_instead_of_looking_whole(self):
        result = {"ok": True, "items": [f"repo-{n}" for n in range(4000)]}
        entry = _trace(_run([[_call("hubs", "{}")]], dispatch=lambda n, a: result,
                            CONTEXT_TOKENS=6000))[0]
        self.assertLess(entry["output"]["model_chars"], entry["output"]["result_chars"])

    def test_the_stored_copy_is_capped_and_says_the_true_length(self):
        result = {"ok": True, "blob": "x" * 3000}
        entry = _trace(_run([[_call("hubs", "{}")]], dispatch=lambda n, a: result,
                            TRACE_OUTPUT_CHARS=200))[0]
        self.assertEqual(len(entry["output"]["text"]), 200)
        self.assertEqual(entry["output"]["shown_chars"], 200)
        self.assertGreater(entry["output"]["model_chars"], 200)

    def test_an_unserializable_result_reports_unknown_not_zero(self):
        entry = _trace(_run([[_call("hubs", "{}")]], dispatch=lambda n, a: {"x": {1, 2}}))[0]
        self.assertIsNone(entry["output"]["result_chars"])

    def test_the_start_and_end_events_carry_the_same_entry_by_seq(self):
        events = _run([[_call("hubs", "{}")]], dispatch=lambda n, a: {"ok": True})
        start = [e for e in events if e["type"] == "tool_start"][0]
        end = [e for e in events if e["type"] == "tool_end"][0]
        self.assertEqual(start["record"]["status"], "running")
        self.assertEqual(start["record"]["seq"], end["record"]["seq"])
        self.assertEqual(end["record"]["status"], "ok")
        self.assertIsNotNone(end["record"]["duration_ms"])

    def test_steps_are_numbered_across_rounds_and_count_attempts_per_tool(self):
        trace = _trace(_run([[_call("hubs", "{}"), _call("hubs", '{"top": 3}', "c2")],
                             [_call("critical_repos", "{}")]],
                            dispatch=lambda n, a: {"ok": True}))
        self.assertEqual([item["seq"] for item in trace], [1, 2, 3])
        self.assertEqual([item["attempt"] for item in trace], [1, 2, 1])
        self.assertEqual([item["iteration"] for item in trace], [1, 1, 2])

    def test_a_rejected_call_is_charged_to_the_tools_lane_not_the_subagent_one(self):
        """Nothing ran, so the lane reserved for an evidence packet must be untouched."""
        entry = _trace(_run([[_call("incident_investigate", "{}")]]))[0]
        self.assertEqual(entry["lane"], "tools")
        self.assertEqual(entry["failure_class"], "bad_arguments")

    def test_the_eval_runner_still_reads_the_tool_name(self):
        """evals/run.py reads `item['tool']` off every entry — keep that key where it was."""
        trace = _trace(_run([[_call("hubs", "{}")]], dispatch=lambda n, a: {"ok": True}))
        self.assertEqual([item["tool"] for item in trace], ["hubs"])


class SchemaIsTheOnlySourceTests(unittest.TestCase):
    """A made-up expectation makes the model change a wrong argument into a confidently wrong one."""

    def test_expected_comes_from_the_tool_schema(self):
        checked = tool_trace.check_arguments("search_code",
                                            {"pattern": "@PostMapping", "max_results": "many"})
        self.assertEqual(checked["invalid"][0]["field"], "max_results")
        self.assertEqual(checked["invalid"][0]["expected"], "integer")

    def test_an_unconstrained_field_says_unknown_rather_than_guessing(self):
        with mock.patch.object(tools, "TOOLS", [{"type": "function", "function": {
                "name": "toy", "parameters": {"type": "object", "properties": {"x": {}},
                                              "required": ["x"]}}}]):
            checked = tool_trace.check_arguments("toy", {})
        self.assertEqual(checked["invalid"][0]["expected"], "unknown")

    def test_an_enum_is_reported_verbatim_when_the_schema_has_one(self):
        with mock.patch.object(tools, "TOOLS", [{"type": "function", "function": {
                "name": "toy", "parameters": {"type": "object", "properties": {
                    "mode": {"type": "string", "enum": ["api", "job"]}}, "required": ["mode"]}}}]):
            checked = tool_trace.check_arguments("toy", {})
        self.assertEqual(checked["invalid"][0]["expected"], "string, one of: api|job")


class FrontendReadsTheSameFieldsTests(unittest.TestCase):
    """The renderer is in another language and another file; a renamed field fails silently there.

    This is the cheapest guard that keeps the panel and the record in step — it reads the shipped
    asset rather than a build of it, because the shipped asset is what the browser gets.
    """

    def setUp(self):
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            self.script = handle.read()
        with open(os.path.join(STATIC, "app.css"), encoding="utf-8") as handle:
            self.style = handle.read()

    def test_the_panel_reads_every_field_the_record_carries(self):
        for field in ("failure_class", "who_can_close", "arguments_raw", "invalid", "notes",
                      "dispatched",
                      "duration_ms", "attempt", "iteration", "seq", "result_chars", "model_chars",
                      "shown_chars"):
            with self.subTest(field=field):
                self.assertIn(field, self.script)

    def test_the_chip_is_a_button_and_opens_a_detail(self):
        self.assertIn("tool-detail", self.script)
        self.assertIn("tool-detail", self.style)
        self.assertRegex(self.script, r"createElement\('button'\)[\s\S]{0,200}tool-chip")

    def test_every_failure_class_has_a_label_in_the_panel(self):
        labels = re.search(r"const FAILURE_LABEL = \{(.*?)\};", self.script, re.DOTALL)
        self.assertIsNotNone(labels)
        named = set(re.findall(r"(\w+):", labels.group(1)))
        self.assertEqual(named, set(tool_trace.FAILURE_CLASSES))

    def test_every_who_can_close_value_has_a_label_in_the_panel(self):
        labels = re.search(r"const WHO_CAN_CLOSE_LABEL = \{(.*?)\};", self.script, re.DOTALL)
        self.assertIsNotNone(labels)
        named = set(re.findall(r"(\w+):", labels.group(1)))
        self.assertLessEqual(set(tool_trace.WHO_CAN_CLOSE.values()), named)


if __name__ == "__main__":
    unittest.main()

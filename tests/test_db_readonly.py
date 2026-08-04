"""The read-only UAT database path, before it has ever reached a database.

RUNBOOK-72's answer came back with the connection blocked at the UAT RDS Proxy, so nothing here can
be verified against a real row. That makes this the right moment to pin the parts that do NOT depend
on their environment — every one of the five MCP rounds was lost on an assertion this side made
about their side (names, then response shapes, then value formats), and every one of them would have
been caught by a test that refused to guess.

What these pin, in the order they matter:

* **Zero database contact when there is nothing to ask.** Unknown, unwired, disabled, out-of-policy:
  each costs no import and no connection. RUNBOOK-71 was exactly this hole one system over.
* **A refusal is never an empty result.** No non-ok packet carries a `rows` key, so there is no
  empty list for a reader to write up as "there is no such record".
* **The column allow-list is the real PII defence.** A column the config never names cannot appear
  in a packet even when the query returns it.
* **An unreadable response shape fails closed** rather than being parsed by guesswork.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retriever import config as retriever_config    # noqa: E402
from webapp import config as webapp_config          # noqa: E402
from webapp import db_readonly, db_registry, tools  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A stand-in for the intranet's scripts/readonly_db.py. It exposes the two functions RUNBOOK-72 §1
# names and nothing else, and every call it receives is written to a state file so a test can assert
# on how many there were — the assertion that matters most here is "none".
_RUNNER_SRC = '''
import json
import os
import sqlite3

STATE = os.environ["FAKE_RUNNER_STATE"]


class OperationalError(Exception):
    """psycopg's class name for the UAT Proxy auth failure (their RUNBOOK-73 §2)."""


class ResultLimitExceeded(Exception):
    """A runner-side refusal: they asked and the runner said no."""


_EXCEPTIONS = {"OperationalError": OperationalError,
               "ResultLimitExceeded": ResultLimitExceeded}


def _state():
    with open(STATE, encoding="utf-8") as handle:
        return json.load(handle)


def _record(entry):
    data = _state()
    data.setdefault("calls", []).append(entry)
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def check_readonly_connection(environment="u"):
    _record({"kind": "check", "environment": environment})
    return _state().get("check", {"ok": True})


def run_readonly_query(sql, params=None, limit=100, environment="u"):
    _record({"kind": "query", "sql": sql, "params": dict(params or {}),
             "limit": limit, "environment": environment})
    data = _state()
    mode = data.get("mode", "return")
    if mode == "raise":
        raise _EXCEPTIONS.get(data.get("error_type"), RuntimeError)(data.get("error", "boom"))
    if mode == "sqlite":
        return _sqlite(data, sql, params, limit)
    return data.get("response")


def _sqlite(data, sql, params, limit):
    """Run the statement for real, so the whole chain is exercised end to end (RUNBOOK-72 §6.6)."""
    translated = sql
    for name in (params or {}):
        translated = translated.replace("%(" + name + ")s", ":" + name)
    translated = translated.replace("schema01.", "")
    conn = sqlite3.connect(data["sqlite_path"])
    try:
        cur = conn.execute(translated + " LIMIT " + str(int(limit)), dict(params or {}))
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()
'''

_SQL = "SELECT use_case_id, channel FROM schema01.tbl_use_case_router WHERE use_case_id = %(uc)s"


def _base_config():
    return {
        "runner": {
            "module_env": "SDLC_DB_SKILL",
            "check_function": "check_readonly_connection",
            "query_function": "run_readonly_query",
            "environment": "u",
            "allowed_environments": ["u"],
            "max_rows_hard_cap": 200,
            "response": {"rows": "?", "columns": "?", "row_format": "?"},
        },
        "queries": {
            "routing": {
                "enabled": True,
                "caller_policy": "product",
                "sql": _SQL,
                "params": {"uc": {"type": "string", "required": True}},
                "max_rows": 10,
                "columns": ["use_case_id", "channel"],
                "source_tables": ["schema01.tbl_use_case_router"],
            },
        },
    }


class _Harness(unittest.TestCase):
    """Temp config + a fake runner on disk; nothing here touches the shipped files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-db-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config_path = os.path.join(self.tmp, "db_queries.json")
        self.state_path = os.path.join(self.tmp, "state.json")
        self.runner_path = os.path.join(self.tmp, "readonly_db.py")
        with open(self.runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_SRC)
        self.write_state({})
        self.write_config(_base_config())
        env = mock.patch.dict(os.environ, {
            "SDLC_DB_QUERIES": self.config_path,
            "SDLC_DB_SKILL": self.runner_path,
            "FAKE_RUNNER_STATE": self.state_path,
        })
        env.start()
        self.addCleanup(env.stop)
        db_readonly._MODULE_CACHE.clear()
        sys.modules.pop("sdlc_intranet_readonly_db", None)
        self.addCleanup(sys.modules.pop, "sdlc_intranet_readonly_db", None)
        enabled = mock.patch.object(webapp_config, "DB_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def write_config(self, payload):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def write_state(self, payload):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def calls(self, kind=None):
        with open(self.state_path, encoding="utf-8") as handle:
            entries = json.load(handle).get("calls", [])
        return [e for e in entries if kind is None or e["kind"] == kind]

    def with_query(self, **overrides):
        payload = _base_config()
        payload["queries"]["routing"].update(overrides)
        self.write_config(payload)


class ZeroContactTests(_Harness):
    """Nothing to ask must cost nothing — no import, no connection, no socket."""

    def test_unknown_query_makes_no_call(self):
        packet = db_readonly.run("no_such_query", {})
        self.assertFalse(packet["ok"])
        self.assertEqual(packet["state"], "refused")
        self.assertEqual(self.calls(), [])
        self.assertNotIn("rows", packet)

    def test_unwired_query_makes_no_call(self):
        self.with_query(sql="?")
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_wired")
        self.assertIn("sql", packet["reason"])
        self.assertEqual(self.calls(), [])

    def test_unwired_columns_are_as_blocking_as_unwired_sql(self):
        # Without a column allow-list there is no way to run this query safely, so it does not run.
        self.with_query(columns=["?"])
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_wired")
        self.assertIn("columns", packet["reason"])
        self.assertEqual(self.calls(), [])

    def test_disabled_query_makes_no_call(self):
        self.with_query(enabled=False)
        self.assertEqual(db_readonly.run("routing", {"uc": "UC1"})["state"], "refused")
        self.assertEqual(self.calls(), [])

    def test_internal_only_query_is_not_reachable_from_the_model(self):
        self.with_query(caller_policy="internal")
        self.write_state({"mode": "return", "response": []})
        packet = db_readonly.run("routing", {"uc": "UC1"}, caller="product")
        self.assertEqual(packet["state"], "refused")
        self.assertIn("internal", packet["reason"])
        self.assertEqual(self.calls(), [])
        # …and the same query IS reachable from an authorised internal caller, so the policy is a
        # gate on WHO asks rather than a way of disabling the query.
        self.assertTrue(db_readonly.run("routing", {"uc": "UC1"}, caller="internal")["ok"])
        self.assertEqual(len(self.calls("query")), 1)

    def test_master_switch_off_makes_no_call(self):
        with mock.patch.object(webapp_config, "DB_ENABLED", False):
            packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "disabled")
        self.assertEqual(self.calls(), [])

    def test_catalog_never_connects(self):
        out = db_readonly.catalog()
        self.assertEqual(self.calls(), [])
        self.assertIn("routing", out["queries"])
        self.assertTrue(out["skill_configured"])

    def test_absent_skill_path_is_not_ready_not_empty(self):
        with mock.patch.dict(os.environ, {"SDLC_DB_SKILL": ""}):
            packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)
        self.assertFalse(packet["means_no_data"])


class StatementGateTests(_Harness):
    """We do not send a statement we would refuse if it came back to us."""

    def assert_refused(self, sql, fragment):
        self.with_query(sql=sql)
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "refused", packet)
        self.assertIn(fragment, packet["reason"])
        self.assertEqual(self.calls(), [], "a refused statement must never be sent")

    def test_dml_is_refused(self):
        self.assert_refused("DELETE FROM schema01.tbl_use_case_router WHERE id = %(uc)s", "DELETE")

    def test_ddl_is_refused(self):
        self.assert_refused("DROP TABLE schema01.tbl_use_case_router", "DROP")

    def test_multi_statement_is_refused(self):
        self.assert_refused(_SQL + "; SELECT b FROM schema01.t2", "more than one statement")

    def test_select_star_is_refused(self):
        self.assert_refused("SELECT * FROM schema01.tbl_use_case_router", "SELECT *")

    def test_unqualified_relation_is_refused(self):
        self.assert_refused("SELECT a FROM tbl_use_case_router", "not schema-qualified")

    def test_comment_is_refused(self):
        self.assert_refused(_SQL + " -- and the rest", "comments")

    def test_locking_read_is_refused(self):
        self.assert_refused(_SQL + " FOR UPDATE", "locking")

    def test_forbidden_function_is_refused(self):
        self.assert_refused("SELECT pg_sleep(10) FROM schema01.t", "pg_sleep")

    def test_positional_parameter_is_refused(self):
        self.assert_refused("SELECT a FROM schema01.t WHERE a = %s", "positional")

    def test_write_into_is_refused(self):
        self.assert_refused("SELECT a INTO schema01.copy_t FROM schema01.t", "INTO")

    def test_schema_qualified_cte_reference_is_accepted(self):
        sql = ("WITH recent AS (SELECT use_case_id, channel FROM schema01.tbl_use_case_router) "
               "SELECT use_case_id, channel FROM recent WHERE use_case_id = %(uc)s")
        self.assertEqual(db_registry.statement_problems(sql), [])

    def test_the_gate_runs_again_before_the_call(self):
        # One definition, two layers (RUNBOOK-71). A plan assembled by other means gets no socket.
        forged = {"name": "routing", "sql": "DELETE FROM schema01.t", "params": {},
                  "max_rows": 5, "columns": ["a"], "source_tables": [], "environment": "u"}
        with mock.patch.object(db_registry, "build_query", return_value=forged):
            packet = db_readonly.run("routing", {})
        self.assertEqual(packet["state"], "refused")
        self.assertEqual(self.calls(), [])


class EnvironmentTests(_Harness):
    def test_non_uat_environment_is_refused(self):
        payload = _base_config()
        payload["runner"]["environment"] = "p"
        self.write_config(payload)
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "refused")
        self.assertIn("allowed_environments", packet["reason"])
        self.assertEqual(self.calls(), [])

    def test_environment_is_not_a_caller_parameter(self):
        packet = db_readonly.run("routing", {"uc": "UC1", "environment": "p"})
        self.assertEqual(packet["state"], "refused")
        self.assertIn("environment", packet["reason"])
        self.assertEqual(self.calls(), [])

    def test_uat_is_what_is_actually_sent(self):
        self.write_state({"mode": "return", "response": []})
        db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual([c["environment"] for c in self.calls("query")], ["u"])

    def test_a_packet_is_never_stamped_uat_when_the_config_points_elsewhere(self):
        # If someone widens allowed_environments, the label has to follow. A packet that says "uat"
        # over data from somewhere else is a false provenance stamp on rows people will quote —
        # worse than no label, because it survives being copied out of context.
        payload = _base_config()
        payload["runner"]["environment"] = "p"
        payload["runner"]["allowed_environments"] = ["u", "p"]
        self.write_config(payload)
        self.write_state({"mode": "return", "response": [{"use_case_id": "UC1", "channel": "SMS"}]})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertTrue(packet["ok"])
        self.assertEqual(packet["environment"], "p")
        self.assertEqual(packet["provenance"], "db:p/routing")
        self.assertIn("no reviewed label", packet["caveat"])
        self.assertFalse(packet["production_verified"])


class FailureIsNotAbsenceTests(_Harness):
    """The LogDream keyword P0, one system over: a refusal must not read like an answer."""

    def test_runner_error_is_error_not_empty(self):
        self.write_state({"mode": "raise",
                          "error": "FATAL: authentication failed host=db.internal password=hunter2"})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "error")
        self.assertNotIn("rows", packet)
        self.assertNotIn("row_count", packet)
        self.assertFalse(packet["means_no_data"])
        self.assertIn("NOT an empty result", packet["hint"])

    def test_a_failure_is_attempted_exactly_once(self):
        # No retry, and above all no second attempt with another account or environment.
        self.write_state({"mode": "raise", "error": "proxy auth"})
        db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(len(self.calls("query")), 1)

    def test_connection_details_never_reach_the_packet(self):
        self.write_state({"mode": "raise",
                          "error": "could not connect to postgres://ro:pw@db.internal:5432/uat "
                                   "password=hunter2 host=db.internal"})
        reason = db_readonly.run("routing", {"uc": "UC1"})["reason"]
        for secret in ("hunter2", "db.internal", "postgres://"):
            self.assertNotIn(secret, reason)

    def test_unreadable_response_shape_fails_closed(self):
        # RUNBOOK-61: their responses are structured and we read them as text. Not again.
        self.write_state({"mode": "return", "response": "2 rows found"})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)
        self.assertIn("runner.response", packet["reason"])

    def test_declared_response_shape_is_read(self):
        payload = _base_config()
        payload["runner"]["response"] = {"rows": "data", "columns": "cols", "row_format": "sequence"}
        self.write_config(payload)
        self.write_state({"mode": "return",
                          "response": {"data": [["UC1", "SMS"]], "cols": ["use_case_id", "channel"]}})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertTrue(packet["ok"])
        self.assertEqual(packet["rows"], [{"use_case_id": "UC1", "channel": "SMS"}])

    def test_declared_response_key_that_is_wrong_fails_closed(self):
        payload = _base_config()
        payload["runner"]["response"] = {"rows": "records", "columns": "?", "row_format": "?"}
        self.write_config(payload)
        self.write_state({"mode": "return", "response": {"data": []}})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)

    def test_a_genuinely_empty_result_says_so(self):
        self.write_state({"mode": "return", "response": []})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertTrue(packet["ok"])
        self.assertEqual(packet["rows"], [])
        self.assertEqual(packet["row_count"], 0)


class ConfirmedRunnerContractTests(_Harness):
    """Their real return shape and failure behaviour, confirmed on the box in RUNBOOK-73.

    Everything here was a guess a round ago. It is pinned now precisely because the previous five
    integration defects were all this: an assumption about their side that nobody had written down.
    """

    REAL_RESPONSE = {"rows": "rows", "columns": "?", "row_format": "dict"}

    def use_real_contract(self, **overrides):
        payload = _base_config()
        payload["runner"]["response"] = dict(self.REAL_RESPONSE)
        payload["runner"]["verify"] = {"ok": "ok", "read_only": "transaction_read_only",
                                       "environment": "environment", "row_count": "row_count"}
        payload["runner"]["not_ready_exceptions"] = ["OperationalError"]
        payload["queries"]["routing"].update(overrides)
        self.write_config(payload)

    def reply(self, rows, **overrides):
        """Their documented envelope: a dict, rows are dicts, and there is NO columns key."""
        envelope = {"ok": True, "environment": "u", "transaction_read_only": True,
                    "row_limit": 10, "row_count": len(rows), "rows": rows}
        envelope.update(overrides)
        self.write_state({"mode": "return", "response": envelope})

    def test_their_documented_envelope_is_read(self):
        self.use_real_contract()
        self.reply([{"use_case_id": "UC1", "channel": "SMS"}])
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertTrue(packet["ok"], packet)
        self.assertEqual(packet["rows"], [{"use_case_id": "UC1", "channel": "SMS"}])
        # Column names come from the row dicts; `columns: "?"` is the right answer, not a gap.
        self.assertEqual(packet["columns"], ["use_case_id", "channel"])

    def test_an_empty_result_invents_no_missing_columns(self):
        self.use_real_contract()
        self.reply([])
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertTrue(packet["ok"])
        self.assertEqual(packet["rows"], [])
        self.assertEqual(packet["columns_missing"], [])

    def test_a_reply_that_says_it_failed_is_not_read_for_rows(self):
        self.use_real_contract()
        self.reply([{"use_case_id": "UC1", "channel": "SMS"}], ok=False)
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)

    def test_a_result_not_proven_read_only_is_discarded(self):
        # They send the proof; checking it is the cheap half. Read-only is the premise of the path.
        self.use_real_contract()
        self.reply([{"use_case_id": "UC1", "channel": "SMS"}], transaction_read_only=False)
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertIn("read-only", packet["reason"])
        self.assertNotIn("rows", packet)

    def test_an_answer_from_another_environment_is_discarded(self):
        self.use_real_contract()
        self.reply([{"use_case_id": "UC1", "channel": "SMS"}], environment="p")
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)

    def test_a_row_count_below_the_rows_read_means_we_read_the_wrong_list(self):
        self.use_real_contract()
        self.reply([{"use_case_id": "UC1"}, {"use_case_id": "UC2"}], row_count=1)
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)

    def test_a_row_count_above_the_rows_read_is_not_treated_as_a_defect(self):
        # Whether they count before or after their LIMIT wrapper is theirs to decide; only the
        # impossible direction is evidence that we are misreading the response.
        self.use_real_contract()
        self.reply([{"use_case_id": "UC1", "channel": "SMS"}], row_count=999)
        self.assertTrue(db_readonly.run("routing", {"uc": "UC1"})["ok"])

    def test_the_proxy_auth_failure_reads_as_not_ready(self):
        self.use_real_contract()
        self.write_state({"mode": "raise", "error_type": "OperationalError",
                          "error": "could not connect"})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "not_ready")
        self.assertNotIn("rows", packet)
        self.assertFalse(packet["means_no_data"])

    def test_a_runner_side_refusal_reads_as_error_not_as_plumbing(self):
        self.use_real_contract()
        self.write_state({"mode": "raise", "error_type": "ResultLimitExceeded",
                          "error": "result over 1MB"})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "error")
        self.assertNotIn("rows", packet)


class SchemaScopeTests(_Harness):
    """A schema list, once configured, is a scope gate — not a spelling check."""

    def with_schemas(self, schemas, sql=None):
        payload = _base_config()
        payload["schemas"] = schemas
        if sql:
            payload["queries"]["routing"]["sql"] = sql
        self.write_config(payload)

    def test_a_schema_outside_the_configured_scope_is_refused(self):
        self.with_schemas(["schema11"])
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["state"], "refused")
        self.assertIn("outside the configured scope", packet["reason"])
        self.assertEqual(self.calls(), [])

    def test_a_configured_schema_is_accepted(self):
        self.with_schemas(["schema01", "schema11"])
        self.write_state({"mode": "return", "response": []})
        self.assertTrue(db_readonly.run("routing", {"uc": "UC1"})["ok"])

    def test_an_unfilled_schema_list_only_requires_qualification(self):
        self.with_schemas(["?"])
        self.write_state({"mode": "return", "response": []})
        self.assertTrue(db_readonly.run("routing", {"uc": "UC1"})["ok"])
        self.assertEqual(db_registry.allowed_schemas(db_registry.load()), [])


class ColumnAllowListTests(_Harness):
    def test_a_column_the_config_never_names_cannot_leave(self):
        self.write_state({"mode": "return", "response": [
            {"use_case_id": "UC1", "channel": "SMS", "customer_mobile": "98765432",
             "customer_name": "Chan Tai Man"}]})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["columns"], ["use_case_id", "channel"])
        self.assertEqual(packet["rows"], [{"use_case_id": "UC1", "channel": "SMS"}])
        self.assertEqual(packet["columns_dropped"], ["customer_mobile", "customer_name"])
        self.assertNotIn("Chan Tai Man", json.dumps(packet, ensure_ascii=False))

    def test_a_declared_column_the_query_did_not_return_is_reported(self):
        self.write_state({"mode": "return", "response": [{"use_case_id": "UC1"}]})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["columns_missing"], ["channel"])

    def test_pii_inside_an_allowed_column_still_crosses_the_exit_gate(self):
        # The allow-list is the first defence, not the only one: a free-text column that IS listed
        # can still carry a phone number, and the exit gate counts what it had to mask.
        self.write_state({"mode": "return",
                          "response": [{"use_case_id": "UC1", "channel": "call 9876 5432"}]})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertNotIn("9876 5432", json.dumps(packet))
        self.assertEqual(packet["sanitized_at_exit"], 1)
        self.assertIn("phone", packet["redaction_kinds"])


class ParameterTests(_Harness):
    def test_unknown_parameter_is_refused_not_dropped(self):
        packet = db_readonly.run("routing", {"uc": "UC1", "limit": 5000})
        self.assertEqual(packet["state"], "refused")
        self.assertIn("limit", packet["reason"])
        self.assertEqual(self.calls(), [])

    def test_missing_required_parameter_is_refused(self):
        packet = db_readonly.run("routing", {})
        self.assertEqual(packet["state"], "refused")
        self.assertEqual(self.calls(), [])

    def test_integer_parameter_is_coerced_and_a_non_integer_is_refused(self):
        payload = _base_config()
        payload["queries"]["routing"]["params"] = {
            "uc": {"type": "string", "required": True},
            "hours": {"type": "integer", "required": False}}
        self.write_config(payload)
        self.write_state({"mode": "return", "response": []})
        db_readonly.run("routing", {"uc": "UC1", "hours": "24"})
        self.assertEqual(self.calls("query")[0]["params"]["hours"], 24)
        self.assertEqual(db_readonly.run("routing", {"uc": "UC1", "hours": "soon"})["state"],
                         "refused")

    def test_values_are_bound_never_interpolated(self):
        self.write_state({"mode": "return", "response": []})
        db_readonly.run("routing", {"uc": "UC1' OR 1=1 --"})
        call = self.calls("query")[0]
        self.assertEqual(call["sql"], _SQL)
        self.assertEqual(call["params"], {"uc": "UC1' OR 1=1 --"})


class RowCapTests(_Harness):
    def test_the_configured_cap_is_what_is_asked_for(self):
        self.write_state({"mode": "return", "response": []})
        db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(self.calls("query")[0]["limit"], 10)

    def test_a_query_cannot_raise_its_own_cap_above_the_hard_cap(self):
        self.with_query(max_rows=100000)
        self.write_state({"mode": "return", "response": []})
        db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(self.calls("query")[0]["limit"], 200)

    def test_over_cap_rows_are_cut_and_reported(self):
        self.with_query(max_rows=2)
        self.write_state({"mode": "return", "response": [
            {"use_case_id": f"UC{i}", "channel": "SMS"} for i in range(5)]})
        packet = db_readonly.run("routing", {"uc": "UC1"})
        self.assertEqual(packet["row_count"], 2)
        self.assertTrue(packet["truncated"])


class EndToEndSqliteTests(_Harness):
    """The whole chain against a real database engine, with none of their names in it."""

    def test_named_query_returns_projected_provenanced_rows(self):
        db_path = os.path.join(self.tmp, "fake.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE tbl_use_case_router "
                     "(use_case_id TEXT, channel TEXT, customer_mobile TEXT)")
        conn.executemany("INSERT INTO tbl_use_case_router VALUES (?, ?, ?)",
                         [("UC1", "SMS", "98765432"), ("UC2", "EMAIL", "91112222")])
        conn.commit()
        conn.close()
        self.write_state({"mode": "sqlite", "sqlite_path": db_path})

        packet = db_readonly.run("routing", {"uc": "UC1"})

        self.assertTrue(packet["ok"])
        self.assertEqual(packet["rows"], [{"use_case_id": "UC1", "channel": "SMS"}])
        self.assertEqual(packet["environment"], "uat")
        self.assertFalse(packet["production_verified"])
        self.assertEqual(packet["source_tables"], ["schema01.tbl_use_case_router"])
        self.assertEqual(packet["provenance"], "db:uat/routing")
        self.assertIn("UAT is NOT production", packet["caveat"])
        self.assertNotIn("98765432", json.dumps(packet))

    def test_check_reports_a_verdict_and_no_connection_details(self):
        self.write_state({"check": {"ok": True, "host": "db.internal", "user": "uat_ro"}})
        verdict = db_readonly.check()
        self.assertTrue(verdict["ok"])
        self.assertNotIn("db.internal", json.dumps(verdict))


class ToolSurfaceTests(_Harness):
    def test_db_query_with_no_name_returns_the_catalog_and_connects_to_nothing(self):
        out = tools.dispatch("db_query", {})
        self.assertEqual(out["state"], "catalog")
        self.assertEqual(self.calls(), [])

    def test_db_query_dispatches_as_the_product_caller(self):
        self.with_query(caller_policy="internal")
        out = tools.dispatch("db_query", {"query": "routing", "params": {"uc": "UC1"}})
        self.assertEqual(out["state"], "refused")
        self.assertEqual(self.calls(), [])

    def test_the_tool_is_declared_with_an_explicit_schema(self):
        schema = next(t for t in tools.TOOLS if t["function"]["name"] == "db_query")
        properties = schema["function"]["parameters"]["properties"]
        self.assertEqual(sorted(properties), ["check", "params", "query"])
        description = schema["function"]["description"]
        self.assertIn("UAT", description)
        self.assertIn("cannot write SQL", description)


class ConfigSourceTests(unittest.TestCase):
    """Which config file is in effect, and whether that choice will survive the next `git pull`.

    The intranet cannot push. So the file they edit must be one git never touches — otherwise their
    edit sits as an uncommitted change to a tracked file and the next pull that also touches it is
    refused, blocking every unrelated fix in the same pull. Making the safe path automatic beats
    documenting it, and making the unsafe state loud beats discovering it weeks later.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-db-src-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "config"))
        env = mock.patch.dict(os.environ, {"SDLC_ROOT": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SDLC_DB_QUERIES", None)
        root = mock.patch.object(retriever_config, "ROOT", self.tmp)
        root.start()
        self.addCleanup(root.stop)

    def write(self, name, payload):
        path = os.path.join(self.tmp, "config", name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_the_local_file_is_picked_up_without_an_env_var(self):
        self.write("db_queries.json", {"queries": {"a": {}}})
        local = self.write("db_queries.local.json", {"queries": {"b": {}}})
        path, kind = db_registry.config_source()
        self.assertEqual((path, kind), (local, "local"))
        self.assertEqual(sorted(db_registry.queries()), ["b"])

    def test_an_explicit_env_var_still_wins(self):
        self.write("db_queries.json", {"queries": {"a": {}}})
        self.write("db_queries.local.json", {"queries": {"b": {}}})
        other = self.write("elsewhere.json", {"queries": {"c": {}}})
        with mock.patch.dict(os.environ, {"SDLC_DB_QUERIES": other}):
            self.assertEqual(db_registry.config_source(), (other, "env"))

    def test_the_tracked_template_is_the_fallback(self):
        template = self.write("db_queries.json", {"queries": {"a": {}}})
        self.assertEqual(db_registry.config_source(), (template, "template"))

    def test_wiring_the_tracked_template_is_reported_loudly(self):
        self.write("db_queries.json",
                   {"queries": {"a": {"sql": "SELECT x FROM s.t", "columns": ["x"]}}})
        warnings = db_registry.config_health()["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("db_queries.local.json", warnings[0])
        self.assertIn("git pull", warnings[0])

    def test_an_all_placeholder_template_is_not_a_warning(self):
        self.write("db_queries.json", {"queries": {"a": {"sql": "?", "columns": ["?"]}}})
        self.assertEqual(db_registry.config_health()["warnings"], [])

    def test_a_local_copy_missing_a_newer_template_query_is_reported(self):
        # Replacement, not merge: a query added to the template later is simply absent on the box.
        # Silent absence is how "the assistant can't do that" becomes a mystery.
        self.write("db_queries.json", {"queries": {"a": {}, "newer": {}}})
        self.write("db_queries.local.json", {"queries": {"a": {}}})
        health = db_registry.config_health()
        self.assertEqual(health["template_only"], ["newer"])
        self.assertIn("newer", health["warnings"][0])

    def test_the_catalog_surfaces_which_file_is_in_effect(self):
        self.write("db_queries.json", {"queries": {}})
        self.write("db_queries.local.json",
                   {"queries": {"a": {"sql": "SELECT x FROM s.t", "columns": ["x"]}}})
        out = db_readonly.catalog()
        self.assertEqual(out["config_source"], "local")
        self.assertTrue(out["config_path"].endswith("db_queries.local.json"))
        self.assertEqual(out["config_warnings"], [])


class ShippedConfigTests(unittest.TestCase):
    """What a fresh clone actually does — the shipped config, not a fixture."""

    def setUp(self):
        # Point explicitly at the TRACKED template. The subject of these tests is what this repo
        # ships, and on the box a `config/db_queries.local.json` exists and would otherwise be
        # picked up — which would turn "what does a fresh clone do" into "what does their box do".
        env = mock.patch.dict(os.environ,
                              {"SDLC_DB_QUERIES": db_registry.template_path()})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = db_registry.load()

    def test_the_shipped_config_parses(self):
        self.assertEqual(self.cfg.get("_load_error", ""), "")
        self.assertTrue(db_registry.queries(self.cfg))

    def test_nothing_is_wired_and_nothing_is_open_to_the_model(self):
        for name, entry in db_registry.readiness(self.cfg).items():
            self.assertEqual(entry["state"], "not_wired", name)
            self.assertEqual(entry["caller_policy"], "internal", name)

    def test_no_real_table_or_column_name_ships_in_this_repo(self):
        for name, spec in db_registry.queries(self.cfg).items():
            self.assertEqual(
                spec.get("sql"), "?",
                f"query {name!r} has a real statement in the git-TRACKED template. If this is the "
                f"box: move your edits to config/db_queries.local.json (gitignored, picked up "
                f"automatically) — an uncommitted change to the tracked file makes the next "
                f"`git pull` refuse to update it and blocks the whole pull.")
            self.assertEqual(spec.get("columns"), ["?"], name)

    def test_the_shipped_environment_is_uat(self):
        self.assertEqual(db_registry.environment(self.cfg), "u")

    def test_the_confirmed_runner_contract_ships_as_the_default(self):
        # Names stay theirs and stay in config; a CONTRACT they have confirmed on the box is a fact
        # a fresh deployment should not have to rediscover.
        runner = db_registry.runner(self.cfg)
        self.assertEqual(runner["response"]["rows"], "rows")
        self.assertEqual(runner["response"]["row_format"], "dict")
        self.assertEqual(runner["response"]["columns"], "?")
        self.assertIn("OperationalError", runner["not_ready_exceptions"])

    def test_no_schema_name_of_theirs_ships_in_this_repo(self):
        self.assertEqual(db_registry.allowed_schemas(self.cfg), [])

    def test_the_intranet_skill_is_never_tracked_by_git(self):
        """It holds their token provider: it must never be COMMITTED. That is not the same as
        "must not exist on disk", and the difference is not academic — the box keeps the skill at
        exactly this path, so the stronger assertion failed on the one machine that has to run it
        (their RUNBOOK-73 §5). Assert the invariant that is actually true: nothing under it is
        tracked, and the path is ignored so nothing can become tracked by accident.
        """
        try:
            tracked = subprocess.run(["git", "ls-files", "--", ".github/skills"], cwd=ROOT,
                                     capture_output=True, text=True, timeout=30)
            ignored = subprocess.run(["git", "check-ignore", "-q", ".github/skills/anything"],
                                     cwd=ROOT, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):    # pragma: no cover - environment-dependent
            self.skipTest("git is not available here")
        if tracked.returncode != 0:                      # pragma: no cover - not a checkout
            self.skipTest("not a git checkout")
        self.assertEqual(tracked.stdout.strip(), "",
                         "the intranet's DB skill must never be tracked by this repo")
        self.assertEqual(ignored.returncode, 0, ".github/skills/ must be gitignored")

    def test_the_master_switch_is_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SDLC_DB_ENABLED", None)
            import importlib
            reloaded = importlib.reload(webapp_config)
            try:
                self.assertFalse(reloaded.DB_ENABLED)
            finally:
                importlib.reload(webapp_config)


if __name__ == "__main__":
    unittest.main()

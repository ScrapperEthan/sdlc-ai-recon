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
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
        raise RuntimeError(data.get("error", "boom"))
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


class ShippedConfigTests(unittest.TestCase):
    """What a fresh clone actually does — the shipped config, not a fixture."""

    def setUp(self):
        clean = mock.patch.dict(os.environ, {}, clear=False)
        clean.start()
        self.addCleanup(clean.stop)
        os.environ.pop("SDLC_DB_QUERIES", None)
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
            self.assertEqual(spec.get("sql"), "?", name)
            self.assertEqual(spec.get("columns"), ["?"], name)

    def test_the_shipped_environment_is_uat(self):
        self.assertEqual(db_registry.environment(self.cfg), "u")

    def test_the_intranet_skill_is_not_in_this_repo(self):
        # It holds their token provider and is git-excluded on the box. If it ever appears here,
        # something copied it out of the box — fail loudly rather than quietly publish it.
        self.assertFalse(os.path.isdir(os.path.join(ROOT, ".github", "skills")))

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

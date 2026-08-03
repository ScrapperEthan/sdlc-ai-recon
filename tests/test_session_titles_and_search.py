"""Session sidebar: AI titles, search, and the investigator panel surviving a reload.

Three separate complaints from the same screen:

* the 事故调查员 panel was live-stream-only, so refreshing the page erased the only part of an
  incident answer that says which production system was contacted;
* every session was labelled with the first 48 characters of its question, which for this app is the
  same prefix over and over and therefore useless for finding one again;
* there was no way to search sessions at all.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from webapp import agent, session_store, session_title, tools
from webapp import server as webserver

_OWNER = "tester-a"


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            session_store.config, "SESSION_STORE",
            os.path.join(self._tmp.name, "chat_sessions.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class SubagentStepPersistenceTests(_StoreCase):
    STEPS = [
        {"type": "subagent_step", "agent": "incident_investigate", "step": "plan",
         "label": "读告警", "detail": {}},
        {"type": "subagent_step", "agent": "incident_investigate", "step": "query",
         "label": "查 cslSmsDeli", "detail": {"server": "logdream", "operation": "log.read",
                                              "elapsed_ms": 812}},
        {"type": "subagent_step", "agent": "incident_investigate", "step": "evidence",
         "label": "命中 3 类异常", "detail": {"raw_ref": "ref-1"}},
    ]

    def test_steps_reload_with_the_session(self):
        session_id = session_store.create_session("t", _OWNER)["id"]
        session_store.append_exchange(session_id, "查一下 3HK 投递失败", "看到 3 类异常。",
                                      owner=_OWNER, subagent_steps=self.STEPS)
        reloaded = session_store.get_session(session_id, _OWNER)
        steps = reloaded["messages"][1]["subagent_steps"]
        self.assertEqual([s["step"] for s in steps], ["plan", "query", "evidence"])
        self.assertEqual(steps[1]["detail"]["server"], "logdream")
        self.assertEqual(steps[2]["detail"]["raw_ref"], "ref-1")

    def test_a_turn_without_an_investigation_stores_an_empty_list(self):
        session_id = session_store.create_session("t", _OWNER)["id"]
        session_store.append_exchange(session_id, "q", "a", owner=_OWNER)
        reloaded = session_store.get_session(session_id, _OWNER)
        self.assertEqual(reloaded["messages"][1]["subagent_steps"], [])

    def test_steps_are_capped_so_the_store_cannot_grow_without_limit(self):
        session_id = session_store.create_session("t", _OWNER)["id"]
        many = [{"type": "subagent_step", "step": "query", "label": str(i), "detail": {}}
                for i in range(session_store.MAX_SUBAGENT_STEPS + 50)]
        session_store.append_exchange(session_id, "q", "a", owner=_OWNER, subagent_steps=many)
        reloaded = session_store.get_session(session_id, _OWNER)
        self.assertEqual(len(reloaded["messages"][1]["subagent_steps"]),
                         session_store.MAX_SUBAGENT_STEPS)

    def test_steps_are_never_replayed_to_the_model(self):
        """The reason it is safe to keep them: `history_for_agent` sends role+content only.

        Progress steps go to the BROWSER on reload. If they reached the model instead, every later
        turn of the conversation would re-spend context on them (and, under the UAT raw-retention
        flag, would carry a reference into retained production log text)."""
        session_id = session_store.create_session("t", _OWNER)["id"]
        session_store.append_exchange(session_id, "q", "a", owner=_OWNER, subagent_steps=self.STEPS)
        replayed = session_store.history_for_agent(session_id, _OWNER)
        self.assertEqual(sorted(replayed[0]), ["content", "role"])
        self.assertNotIn("logdream", json.dumps(replayed))
        self.assertNotIn("ref-1", json.dumps(replayed))

    def test_the_agent_carries_the_steps_into_its_done_event(self):
        """Relaying them live is not enough — the terminal event is what the server persists."""
        fake = [{"type": "subagent_step", "step": "plan", "label": "读告警", "detail": {}},
                {"type": "subagent_step", "step": "evidence", "label": "命中", "detail": {}},
                {"type": "result", "packet": {"ok": True, "evidence": []}}]
        calls = [{"id": "c1", "type": "function",
                  "function": {"name": "incident_investigate",
                               "arguments": '{"alert_text": "x"}'}}]
        replies = iter([{"role": "assistant", "content": None, "tool_calls": calls},
                        {"role": "assistant", "content": "done"}])

        def _chat(messages, tool_list=None):
            yield ("final", next(replies))

        with mock.patch.object(agent.llm, "chat_stream", _chat), \
             mock.patch.object(agent.llm, "stream_text", lambda m: []), \
             mock.patch.object(tools, "dispatch_events", lambda n, a, owner="": iter(fake)):
            events = list(agent.answer_events("q"))
        done = [e for e in events if e.get("type") == "done"][0]
        self.assertEqual([s["step"] for s in done["subagent_steps"]], ["plan", "evidence"])
        self.assertTrue(all(s["agent"] == "incident_investigate" for s in done["subagent_steps"]))
        # and the blocking wrapper surfaces the same list
        with mock.patch.object(agent, "answer_events", lambda q, h=None: iter(events)):
            self.assertEqual(len(agent.answer("q")["subagent_steps"]), 2)


class SessionTitleTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(session_title.config, "LLM_MOCK", False),
            mock.patch.object(session_title.config, "SESSION_TITLE_LLM", True),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()

    def _reply(self, content):
        return mock.patch.object(session_title.llm, "chat",
                                 lambda *a, **k: {"role": "assistant", "content": content})

    def test_uses_the_model_answer(self):
        with self._reply("3HK SMS 投递失败排查"):
            self.assertEqual(session_title.summarize("q", "a"), "3HK SMS 投递失败排查")

    def test_strips_the_wrappers_models_add(self):
        for raw, expected in [
            ('"3HK 投递失败"', "3HK 投递失败"),
            ("标题：3HK 投递失败", "3HK 投递失败"),
            ("Title: 3HK delivery failure", "3HK delivery failure"),
            ("**3HK 投递失败**", "3HK 投递失败"),
            ("3HK 投递失败。", "3HK 投递失败"),
            ("3HK 投递失败\n（这是一个标题）", "3HK 投递失败"),
        ]:
            with self.subTest(raw=raw), self._reply(raw):
                self.assertEqual(session_title.summarize("q"), expected)

    def test_a_long_title_is_capped(self):
        with self._reply("很" * 200):
            title = session_title.summarize("q")
        self.assertLessEqual(len(title), session_title.MAX_TITLE_CHARS)

    def test_a_model_failure_falls_back_to_the_question(self):
        def _boom(*a, **k):
            raise RuntimeError("endpoint down")
        with mock.patch.object(session_title.llm, "chat", _boom):
            self.assertEqual(session_title.summarize("who consumes topic X?"),
                             "who consumes topic X?")

    def test_an_empty_or_junk_reply_falls_back(self):
        for junk in ("", "   ", "「」", None):
            with self.subTest(junk=junk), self._reply(junk):
                self.assertEqual(session_title.summarize("who consumes topic X?"),
                                 "who consumes topic X?")

    def test_mock_mode_never_calls_the_model(self):
        def _boom(*a, **k):
            raise AssertionError("must not call the model in LLM_MOCK")
        with mock.patch.object(session_title.config, "LLM_MOCK", True), \
             mock.patch.object(session_title.llm, "chat", _boom):
            self.assertEqual(session_title.summarize("q1"), "q1")

    def test_the_feature_flag_turns_it_off(self):
        def _boom(*a, **k):
            raise AssertionError("must not call the model when the flag is off")
        with mock.patch.object(session_title.config, "SESSION_TITLE_LLM", False), \
             mock.patch.object(session_title.llm, "chat", _boom):
            self.assertEqual(session_title.summarize("q1"), "q1")

    def test_fallback_collapses_whitespace_and_truncates(self):
        self.assertEqual(session_title.fallback_title("  a\n\n  b  "), "a b")
        self.assertEqual(session_title.fallback_title(""), "New session")
        long_title = session_title.fallback_title("x" * 200)
        self.assertEqual(len(long_title), session_title.MAX_TITLE_CHARS)
        self.assertTrue(long_title.endswith("..."))


class TitleOnTheStoreTests(_StoreCase):
    def test_the_supplied_title_wins_on_the_first_exchange(self):
        session_id = session_store.create_session("New session", _OWNER)["id"]
        session = session_store.append_exchange(
            session_id, "一段很长的提问，前 48 个字符跟别的会话完全一样……", "a",
            owner=_OWNER, title="3HK SMSC 投递失败")
        self.assertEqual(session["title"], "3HK SMSC 投递失败")

    def test_a_later_exchange_never_renames_the_session(self):
        """Otherwise a session would move under the user mid-conversation."""
        session_id = session_store.create_session("New session", _OWNER)["id"]
        session_store.append_exchange(session_id, "q1", "a1", owner=_OWNER, title="第一个话题")
        session = session_store.append_exchange(session_id, "q2", "a2", owner=_OWNER,
                                                title="第二个话题")
        self.assertEqual(session["title"], "第一个话题")

    def test_no_title_keeps_the_old_truncation_behaviour(self):
        session_id = session_store.create_session("New session", _OWNER)["id"]
        for title in (None, "", "   "):
            with self.subTest(title=title):
                fresh = session_store.create_session("New session", _OWNER)["id"]
                session = session_store.append_exchange(fresh, "who consumes topic X?", "a",
                                                        owner=_OWNER, title=title)
                self.assertEqual(session["title"], "who consumes topic X?")
        self.assertTrue(session_id)


class SessionSearchTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.a = session_store.create_session("New session", _OWNER)["id"]
        session_store.append_exchange(
            self.a, "3HK 的 SMS 用例昨晚开始投递失败", "cslSmsDeli 报了 SocketTimeout。",
            owner=_OWNER, title="3HK SMSC 投递失败")
        self.b = session_store.create_session("New session", _OWNER)["id"]
        session_store.append_exchange(
            self.b, "Aurora push 的下游有哪些仓库", "共 12 个。", owner=_OWNER,
            title="Aurora push 下游仓库")
        self.other = session_store.create_session("New session", "tester-b")["id"]
        session_store.append_exchange(self.other, "3HK 也问一句", "a", owner="tester-b",
                                      title="3HK 别人的会话")

    def _ids(self, query, owner=_OWNER):
        return [hit["id"] for hit in session_store.search_sessions(owner, query)]

    def test_matches_the_title(self):
        self.assertEqual(self._ids("Aurora"), [self.b])

    def test_matches_message_text_the_title_does_not_mention(self):
        self.assertEqual(self._ids("SocketTimeout"), [self.a])

    def test_is_case_insensitive(self):
        self.assertEqual(self._ids("sockettimeout"), [self.a])
        self.assertEqual(self._ids("AURORA"), [self.b])

    def test_reports_where_it_matched_and_the_surrounding_text(self):
        hit = session_store.search_sessions(_OWNER, "SocketTimeout")[0]
        self.assertEqual(hit["match"]["in"], ["assistant"])
        self.assertIn("SocketTimeout", hit["match"]["snippet"])
        title_hit = session_store.search_sessions(_OWNER, "Aurora push 下游")[0]
        self.assertEqual(title_hit["match"]["in"], ["title"])

    def test_a_term_in_both_title_and_body_reports_both(self):
        hit = session_store.search_sessions(_OWNER, "3HK")[0]
        self.assertEqual(hit["match"]["in"], ["title", "user"])

    def test_the_snippet_is_a_single_line_excerpt_not_the_whole_message(self):
        long_session = session_store.create_session("New session", _OWNER)["id"]
        session_store.append_exchange(long_session, "q",
                                      ("x" * 400) + "\nNEEDLE\n" + ("y" * 400), owner=_OWNER)
        hit = session_store.search_sessions(_OWNER, "NEEDLE")[0]
        snippet = hit["match"]["snippet"]
        self.assertIn("NEEDLE", snippet)
        self.assertNotIn("\n", snippet)
        self.assertLess(len(snippet), 200)
        self.assertTrue(snippet.startswith("...") and snippet.endswith("..."))

    def test_search_is_owner_scoped(self):
        self.assertEqual(self._ids("3HK"), [self.a])          # not the other tester's 3HK session
        self.assertEqual(self._ids("3HK", "tester-b"), [self.other])
        self.assertEqual(self._ids("3HK", ""), [])            # no cookie yet => nothing

    def test_a_blank_query_returns_nothing_rather_than_everything(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.assertEqual(session_store.search_sessions(_OWNER, blank), [])

    def test_no_match_is_an_empty_list(self):
        self.assertEqual(self._ids("HSBCnet"), [])

    def test_hits_are_newest_first_and_capped(self):
        ids = self._ids("投递")   # both a and (title of) a only; add a broader term
        self.assertTrue(ids)
        broad = session_store.search_sessions(_OWNER, "e", limit=1)
        self.assertLessEqual(len(broad), 1)


class _Browser:
    """One browser talking to the test server: keeps its `sdlc_uid` cookie across calls, so the
    owner scoping the store enforces is exercised rather than bypassed."""

    def __init__(self, base):
        self.base = base
        self.cookie = None

    def _open(self, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data,
                                         method="POST" if data else "GET")
        if data:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, raw, set_cookie = (response.status, response.read(),
                                           response.headers.get("Set-Cookie"))
        except urllib.error.HTTPError as error:
            status, raw, set_cookie = error.code, error.read(), None
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        return status, raw

    def get(self, path):
        status, raw = self._open(path)
        return status, json.loads(raw.decode("utf-8"))

    def post(self, path, payload):
        status, raw = self._open(path, payload)
        return status, json.loads(raw.decode("utf-8"))

    def stream(self, path, payload):
        """NDJSON events from /api/chat/stream, in order."""
        _status, raw = self._open(path, payload)
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


class ServerChatWiringTests(_StoreCase):
    """Over real sockets: the two chat routes must BOTH persist the investigator steps and ask for a
    title, and they take different code paths (streaming vs blocking) to do it."""

    ANSWER = "cslSmsDeli 报了 SocketTimeout。"
    STEPS = [{"type": "subagent_step", "agent": "incident_investigate", "step": "evidence",
              "label": "命中 3 类异常", "detail": {"server": "logdream", "operation": "log.read"}}]

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), webserver.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        host, port = self.srv.server_address[:2]
        self.browser = _Browser(f"http://{host}:{port}")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _fake_turn(self):
        """agent.answer_events -> one done event carrying an investigation."""
        def _events(question, history=None, owner=""):
            yield {"type": "done", "answer": self.ANSWER, "tool_trace": [], "usage": None,
                   "citations": None, "views": [], "subagent_steps": self.STEPS}
        return _events

    def _titled(self, title="3HK SMSC 投递失败"):
        return mock.patch.object(webserver.session_title, "summarize",
                                 lambda question, answer="": title)

    def test_the_streaming_route_persists_steps_and_the_title(self):
        with mock.patch.object(webserver.agent, "answer_events", self._fake_turn()), self._titled():
            events = self.browser.stream("/api/chat/stream", {"question": "3HK 投递失败？"})
        done = [e for e in events if e["type"] == "done"][0]
        session_id = done["session"]["id"]
        self.assertEqual(done["session"]["title"], "3HK SMSC 投递失败")

        _status, detail = self.browser.get(f"/api/sessions/{session_id}")
        self.assertEqual([s["step"] for s in detail["messages"][1]["subagent_steps"]], ["evidence"])

    def test_the_blocking_route_persists_steps_and_the_title(self):
        result = {"answer": self.ANSWER, "tool_trace": [], "usage": None, "citations": None,
                  "views": [], "subagent_steps": self.STEPS}
        with mock.patch.object(webserver.agent, "answer", lambda q, h=None: result), \
             self._titled("Aurora push 下游"):
            _status, body = self.browser.post("/api/chat", {"question": "Aurora 下游？"})
        self.assertEqual(body["session"]["title"], "Aurora push 下游")
        _status, detail = self.browser.get(f"/api/sessions/{body['session']['id']}")
        self.assertEqual([s["step"] for s in detail["messages"][1]["subagent_steps"]], ["evidence"])

    def test_a_second_turn_does_not_ask_for_another_title(self):
        """One title call per session, not per turn — this is the whole cost story."""
        asked = []

        def _summarize(question, answer=""):
            asked.append(question)
            return "第一个话题"

        with mock.patch.object(webserver.agent, "answer_events", self._fake_turn()), \
             mock.patch.object(webserver.session_title, "summarize", _summarize):
            first = self.browser.stream("/api/chat/stream", {"question": "q1"})
            session_id = [e for e in first if e["type"] == "done"][0]["session"]["id"]
            self.browser.stream("/api/chat/stream",
                                {"question": "q2", "session_id": session_id})
        self.assertEqual(asked, ["q1"])

    def test_search_route_shares_the_listing_shape_and_is_owner_scoped(self):
        with mock.patch.object(webserver.agent, "answer_events", self._fake_turn()), \
             self._titled("3HK SMSC 投递失败"):
            self.browser.stream("/api/chat/stream", {"question": "3HK 投递失败？"})

        _status, hit = self.browser.get("/api/sessions?q=SocketTimeout")
        self.assertEqual(hit["query"], "SocketTimeout")
        self.assertEqual([s["title"] for s in hit["sessions"]], ["3HK SMSC 投递失败"])
        self.assertEqual(hit["sessions"][0]["match"]["in"], ["assistant"])

        _status, miss = self.browser.get("/api/sessions?q=HSBCnet")
        self.assertEqual(miss["sessions"], [])

        # no query => the plain listing, unchanged
        _status, listing = self.browser.get("/api/sessions")
        self.assertEqual(len(listing["sessions"]), 1)
        self.assertNotIn("match", listing["sessions"][0])

        # a different browser (its own cookie) sees none of it
        _status, other = _Browser(self.browser.base).get("/api/sessions?q=SocketTimeout")
        self.assertEqual(other["sessions"], [])

    def test_a_title_call_that_fails_still_saves_the_answer(self):
        """A cosmetic label must never be able to cost the user the answer."""
        def _boom(*a, **k):
            raise RuntimeError("model down")

        with mock.patch.object(webserver.agent, "answer_events", self._fake_turn()), \
             mock.patch.object(webserver.session_title.llm, "chat", _boom), \
             mock.patch.object(webserver.session_title.config, "LLM_MOCK", False):
            events = self.browser.stream("/api/chat/stream", {"question": "who consumes topic X?"})
        done = [e for e in events if e["type"] == "done"][0]
        self.assertEqual(done["answer"], self.ANSWER)
        self.assertEqual(done["session"]["title"], "who consumes topic X?")   # the truncation


if __name__ == "__main__":
    unittest.main()

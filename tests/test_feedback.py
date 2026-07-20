import os
import tempfile
import unittest
from unittest import mock

from webapp import session_store

_OWNER = "tester-a"


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._store = os.path.join(self._tmp.name, "chat_sessions.json")
        self._patch = mock.patch.object(session_store.config, "SESSION_STORE", self._store)
        self._patch.start()
        session = session_store.create_session("t", _OWNER)
        self.session_id = session["id"]
        session_store.append_exchange(
            self.session_id, "who consumes X?", "repo A does.", owner=_OWNER)
        # messages: [0]=user, [1]=assistant

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_up_vote_attaches_and_reloads(self):
        result = session_store.set_feedback(self.session_id, 1, "up", owner=_OWNER)
        self.assertEqual(result["vote"], "up")
        detail = session_store.get_session(self.session_id, _OWNER)
        self.assertEqual(detail["messages"][1]["feedback"]["vote"], "up")
        self.assertIsNone(detail["messages"][0]["feedback"])

    def test_down_vote_with_comment_is_listed_flat(self):
        session_store.set_feedback(self.session_id, 1, "down", "  missed repo B  ", owner=_OWNER)
        entries = session_store.list_feedback(_OWNER)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["vote"], "down")
        self.assertEqual(entry["comment"], "missed repo B")  # trimmed
        self.assertEqual(entry["question"], "who consumes X?")
        self.assertEqual(entry["answer"], "repo A does.")
        self.assertEqual(entry["message_index"], 1)

    def test_empty_vote_clears(self):
        session_store.set_feedback(self.session_id, 1, "up", owner=_OWNER)
        self.assertIsNone(session_store.set_feedback(self.session_id, 1, "", owner=_OWNER))
        self.assertEqual(session_store.list_feedback(_OWNER), [])

    def test_rejects_invalid_vote(self):
        with self.assertRaises(ValueError):
            session_store.set_feedback(self.session_id, 1, "sideways", owner=_OWNER)

    def test_rejects_non_assistant_message(self):
        with self.assertRaises(ValueError):
            session_store.set_feedback(self.session_id, 0, "up", owner=_OWNER)  # index 0 is the user turn

    def test_rejects_out_of_range_index(self):
        with self.assertRaises(IndexError):
            session_store.set_feedback(self.session_id, 99, "up", owner=_OWNER)

    def test_rejects_unknown_session(self):
        with self.assertRaises(KeyError):
            session_store.set_feedback("nope", 1, "up", owner=_OWNER)


class SessionOwnershipTests(unittest.TestCase):
    """RUNBOOK-43: sessions/feedback used to be global — any caller could list, read, or vote on
    any other user's chat via `GET /api/sessions`, `GET /api/sessions/<id>`, `GET /api/feedback`.
    Each session now carries an `owner` (an opaque per-browser id, see server.py `_resolve_uid`) and
    every read/write is scoped to it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._store = os.path.join(self._tmp.name, "chat_sessions.json")
        self._patch = mock.patch.object(session_store.config, "SESSION_STORE", self._store)
        self._patch.start()
        self.mine = session_store.create_session("mine", "owner-a")["id"]
        session_store.append_exchange(self.mine, "q1", "a1", owner="owner-a")
        self.theirs = session_store.create_session("theirs", "owner-b")["id"]
        session_store.append_exchange(self.theirs, "q2", "a2", owner="owner-b")
        session_store.set_feedback(self.theirs, 1, "down", "wrong", owner="owner-b")

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_list_sessions_only_returns_own(self):
        mine = session_store.list_sessions("owner-a")
        self.assertEqual([s["id"] for s in mine], [self.mine])
        theirs = session_store.list_sessions("owner-b")
        self.assertEqual([s["id"] for s in theirs], [self.theirs])

    def test_get_session_denies_other_owner(self):
        session_store.get_session(self.mine, "owner-a")  # own session: fine
        with self.assertRaises(KeyError):
            session_store.get_session(self.mine, "owner-b")  # someone else's: not found, not 403

    def test_history_for_agent_denies_other_owner(self):
        with self.assertRaises(KeyError):
            session_store.history_for_agent(self.theirs, "owner-a")

    def test_append_exchange_denies_other_owner(self):
        with self.assertRaises(KeyError):
            session_store.append_exchange(self.mine, "q", "a", owner="owner-b")

    def test_set_feedback_denies_other_owner(self):
        with self.assertRaises(KeyError):
            session_store.set_feedback(self.mine, 1, "up", owner="owner-b")

    def test_list_feedback_only_returns_own(self):
        self.assertEqual(session_store.list_feedback("owner-a"), [])
        theirs = session_store.list_feedback("owner-b")
        self.assertEqual(len(theirs), 1)
        self.assertEqual(theirs[0]["session_id"], self.theirs)

    def test_no_owner_never_matches(self):
        # a caller with no cookie yet (owner="") must not see anything, including other empty-owner
        # rows -- _owned() requires owner to be truthy, closing the "everyone is ''" loophole.
        self.assertEqual(session_store.list_sessions(""), [])
        with self.assertRaises(KeyError):
            session_store.get_session(self.mine, "")


if __name__ == "__main__":
    unittest.main()

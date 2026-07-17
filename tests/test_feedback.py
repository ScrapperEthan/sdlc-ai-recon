import os
import tempfile
import unittest
from unittest import mock

from webapp import session_store


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._store = os.path.join(self._tmp.name, "chat_sessions.json")
        self._patch = mock.patch.object(session_store.config, "SESSION_STORE", self._store)
        self._patch.start()
        session = session_store.create_session("t")
        self.session_id = session["id"]
        session_store.append_exchange(self.session_id, "who consumes X?", "repo A does.")
        # messages: [0]=user, [1]=assistant

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_up_vote_attaches_and_reloads(self):
        result = session_store.set_feedback(self.session_id, 1, "up")
        self.assertEqual(result["vote"], "up")
        detail = session_store.get_session(self.session_id)
        self.assertEqual(detail["messages"][1]["feedback"]["vote"], "up")
        self.assertIsNone(detail["messages"][0]["feedback"])

    def test_down_vote_with_comment_is_listed_flat(self):
        session_store.set_feedback(self.session_id, 1, "down", "  missed repo B  ")
        entries = session_store.list_feedback()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["vote"], "down")
        self.assertEqual(entry["comment"], "missed repo B")  # trimmed
        self.assertEqual(entry["question"], "who consumes X?")
        self.assertEqual(entry["answer"], "repo A does.")
        self.assertEqual(entry["message_index"], 1)

    def test_empty_vote_clears(self):
        session_store.set_feedback(self.session_id, 1, "up")
        self.assertIsNone(session_store.set_feedback(self.session_id, 1, ""))
        self.assertEqual(session_store.list_feedback(), [])

    def test_rejects_invalid_vote(self):
        with self.assertRaises(ValueError):
            session_store.set_feedback(self.session_id, 1, "sideways")

    def test_rejects_non_assistant_message(self):
        with self.assertRaises(ValueError):
            session_store.set_feedback(self.session_id, 0, "up")  # index 0 is the user turn

    def test_rejects_out_of_range_index(self):
        with self.assertRaises(IndexError):
            session_store.set_feedback(self.session_id, 99, "up")

    def test_rejects_unknown_session(self):
        with self.assertRaises(KeyError):
            session_store.set_feedback("nope", 1, "up")


if __name__ == "__main__":
    unittest.main()

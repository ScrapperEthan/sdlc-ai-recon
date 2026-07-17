import os
import tempfile
import threading
import unittest
from unittest import mock

from webapp import config, llm_routes


class ConfigOverrideTests(unittest.TestCase):
    def tearDown(self):
        config.set_llm_override(None)

    def test_override_wins_then_falls_back(self):
        default = config.llm_default_base_url()
        token = config.set_llm_override({"base_url": "http://127.0.0.1:24101/v1", "model": "m2"})
        self.assertEqual(config.LLM_BASE_URL, "http://127.0.0.1:24101/v1")
        self.assertEqual(config.LLM_MODEL, "m2")
        config.reset_llm_override(token)
        self.assertEqual(config.LLM_BASE_URL, default)

    def test_empty_override_field_falls_back_to_default(self):
        default_model = config.LLM_MODEL
        config.set_llm_override({"base_url": "http://127.0.0.1:24101/v1"})  # no model key
        self.assertEqual(config.LLM_BASE_URL, "http://127.0.0.1:24101/v1")
        self.assertEqual(config.LLM_MODEL, default_model)  # unset field -> env default

    def test_override_is_thread_isolated(self):
        """A per-request override in one thread must not leak into another user's thread."""
        config.set_llm_override({"base_url": "http://127.0.0.1:1111/v1"})
        seen = {}

        def worker():
            seen["before_set"] = config.LLM_BASE_URL          # should be the default, not :1111
            config.set_llm_override({"base_url": "http://127.0.0.1:2222/v1"})
            seen["after_set"] = config.LLM_BASE_URL

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertEqual(seen["before_set"], config.llm_default_base_url())
        self.assertEqual(seen["after_set"], "http://127.0.0.1:2222/v1")
        self.assertEqual(config.LLM_BASE_URL, "http://127.0.0.1:1111/v1")  # main thread unchanged


class LlmRoutesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        store = os.path.join(self._tmp.name, "llm_routes.json")
        self._patches = [
            mock.patch.object(config, "LLM_ROUTES_STORE", store),
            mock.patch.object(config, "LLM_ALLOW_NONLOOPBACK", False),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_register_loopback_and_resolve(self):
        record = llm_routes.register("http://127.0.0.1:24101/v1/", model="gpt-5.5",
                                     api_key="secret", label="alice")
        self.assertTrue(record["token"])
        self.assertEqual(record["base_url"], "http://127.0.0.1:24101/v1")  # trailing slash trimmed
        self.assertTrue(record["has_api_key"])
        self.assertNotIn("api_key", record)  # secret never handed back to the browser

        override = llm_routes.resolve(record["token"])
        self.assertEqual(override["base_url"], "http://127.0.0.1:24101/v1")
        self.assertEqual(override["model"], "gpt-5.5")
        self.assertEqual(override["api_key"], "secret")

    def test_non_loopback_is_rejected(self):
        with self.assertRaises(ValueError):
            llm_routes.register("http://10.0.0.5:4141/v1", label="evil")
        with self.assertRaises(ValueError):
            llm_routes.register("http://alice-pc.corp:4141/v1")

    def test_non_http_scheme_rejected(self):
        with self.assertRaises(ValueError):
            llm_routes.register("file:///etc/passwd")

    def test_unknown_token_resolves_none(self):
        self.assertIsNone(llm_routes.resolve("nope"))
        self.assertIsNone(llm_routes.resolve(""))
        self.assertEqual(llm_routes.describe("nope"), {"registered": False})

    def test_two_users_route_to_their_own_endpoints(self):
        alice = llm_routes.register("http://127.0.0.1:24101/v1", label="alice")
        bob = llm_routes.register("http://localhost:24102/v1", label="bob")
        self.assertNotEqual(alice["token"], bob["token"])
        self.assertEqual(llm_routes.resolve(alice["token"])["base_url"], "http://127.0.0.1:24101/v1")
        self.assertEqual(llm_routes.resolve(bob["token"])["base_url"], "http://localhost:24102/v1")

    def test_nonloopback_allowed_when_opted_in(self):
        with mock.patch.object(config, "LLM_ALLOW_NONLOOPBACK", True):
            record = llm_routes.register("http://bastion.internal:9000/v1", label="ops")
        self.assertEqual(record["base_url"], "http://bastion.internal:9000/v1")


if __name__ == "__main__":
    unittest.main()

import builtins
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

from webapp import config, llm_routes, llm, llm_credentials
from webapp import server as webserver
from webapp.llm_providers import copilot_responses, openai_chat, github_copilot_direct


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


# ===================================================================================================
# Internal-beta paste-token "direct Copilot" mode (SDLC_LLM_TOKEN_MODE).
# See docs/specs/copilot-token-direct-mode.md. THROWAWAY feature -- these tests (and the four small
# hunks they cover in config.py/llm.py/server.py) are meant to delete cleanly together with
# webapp/llm_credentials.py and webapp/llm_providers/github_copilot_direct.py before GA.
# ===================================================================================================


class ProviderOverrideTests(unittest.TestCase):
    """4a: `provider` (and `credential_id`) now resolve per-request the same way base_url/model do."""

    def tearDown(self):
        config.set_llm_override(None)

    def test_provider_override_wins_then_falls_back(self):
        default_provider = config.LLM_PROVIDER
        token = config.set_llm_override({"provider": "github_copilot_direct", "credential_id": "abc"})
        self.assertEqual(config.LLM_PROVIDER, "github_copilot_direct")
        self.assertEqual(config.LLM_CREDENTIAL_ID, "abc")
        config.reset_llm_override(token)
        self.assertEqual(config.LLM_PROVIDER, default_provider)
        self.assertEqual(config.LLM_CREDENTIAL_ID, "")  # unset -> env default (blank)

    def test_tunnel_style_override_without_provider_key_is_unaffected(self):
        """A plain tunnel override (base_url/model only, as today) must NOT accidentally change the
        provider -- tunnel users always get the env-default provider, exactly like before this
        feature existed."""
        default_provider = config.LLM_PROVIDER
        token = config.set_llm_override({"base_url": "http://127.0.0.1:24101/v1"})
        try:
            self.assertEqual(config.LLM_PROVIDER, default_provider)
            self.assertEqual(config.LLM_CREDENTIAL_ID, "")
        finally:
            config.reset_llm_override(token)


class ProviderModuleSelectionTests(unittest.TestCase):
    """llm.py `_provider_module()` becomes override-aware and knows github_copilot_direct (4a)."""

    def tearDown(self):
        config.set_llm_override(None)

    def test_selects_each_known_provider_via_override(self):
        cases = {
            "copilot_responses": copilot_responses,
            "openai_chat": openai_chat,
            "github_copilot_direct": github_copilot_direct,
        }
        for name, module in cases.items():
            token = config.set_llm_override({"provider": name})
            try:
                self.assertIs(llm._provider_module(), module)
            finally:
                config.reset_llm_override(token)

    def test_no_override_uses_env_default(self):
        self.assertIs(llm._provider_module(), copilot_responses)  # this repo's env default

    def test_unknown_provider_raises(self):
        token = config.set_llm_override({"provider": "nope"})
        try:
            with self.assertRaises(RuntimeError):
                llm._provider_module()
        finally:
            config.reset_llm_override(token)


class ConcurrentProviderIsolationTests(unittest.TestCase):
    """Acceptance criterion #3: two concurrent users (one tunnel, one token) each get the right
    provider -- no leakage across contextvars. Mirrors ConfigOverrideTests.test_override_is_thread_
    isolated but for provider+credential_id, with real concurrent threads (not sequential calls)."""

    def tearDown(self):
        config.set_llm_override(None)

    def test_two_concurrent_overrides_resolve_independently(self):
        results = {}
        start_gate = threading.Barrier(2)

        def tunnel_worker():
            token = config.set_llm_override({"base_url": "http://127.0.0.1:24101/v1"})
            try:
                start_gate.wait(timeout=5)  # line the two threads up so they genuinely overlap
                time.sleep(0.05)
                results["tunnel_provider"] = config.LLM_PROVIDER
                results["tunnel_credential_id"] = config.LLM_CREDENTIAL_ID
                results["tunnel_module"] = llm._provider_module()
            finally:
                config.reset_llm_override(token)

        def token_worker():
            token = config.set_llm_override(
                {"mode": "copilot_token", "provider": "github_copilot_direct",
                 "credential_id": "cred-xyz"}
            )
            try:
                start_gate.wait(timeout=5)
                time.sleep(0.05)
                results["token_provider"] = config.LLM_PROVIDER
                results["token_credential_id"] = config.LLM_CREDENTIAL_ID
                results["token_module"] = llm._provider_module()
            finally:
                config.reset_llm_override(token)

        t1 = threading.Thread(target=tunnel_worker)
        t2 = threading.Thread(target=token_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(results["tunnel_provider"], config.llm_default_provider())
        self.assertEqual(results["tunnel_credential_id"], "")
        self.assertIs(results["tunnel_module"], copilot_responses)

        self.assertEqual(results["token_provider"], "github_copilot_direct")
        self.assertEqual(results["token_credential_id"], "cred-xyz")
        self.assertIs(results["token_module"], github_copilot_direct)

        # main thread's own (unset) context is untouched by either worker
        self.assertIsNone(config._llm_override.get())


class LlmCredentialsStoreTests(unittest.TestCase):
    """4c: webapp/llm_credentials.py -- RAM-only, connect/resolve/disconnect lifecycle, fails closed
    on a stale id, and genuinely never touches disk."""

    def setUp(self):
        self._ids = []

    def tearDown(self):
        for cred_id in self._ids:
            llm_credentials.disconnect(cred_id)

    def _connect(self, token="pasted-secret-token", owner_uid="u1"):
        cred_id = llm_credentials.connect(token, owner_uid=owner_uid)
        self._ids.append(cred_id)
        return cred_id

    def test_connect_returns_opaque_id_distinct_per_call(self):
        a = self._connect()
        b = self._connect()
        self.assertTrue(a)
        self.assertTrue(b)
        self.assertNotEqual(a, b)

    def test_connect_blank_token_raises(self):
        with self.assertRaises(ValueError):
            llm_credentials.connect("")
        with self.assertRaises(ValueError):
            llm_credentials.connect("   ")

    def test_resolve_returns_the_stored_record(self):
        cred_id = self._connect(token="my-oauth-token", owner_uid="alice")
        record = llm_credentials.resolve(cred_id)
        self.assertEqual(record["oauth_token"], "my-oauth-token")
        self.assertEqual(record["owner_uid"], "alice")
        self.assertIsNone(record["service_token"])

    def test_resolve_unknown_or_blank_id_is_none(self):
        self.assertIsNone(llm_credentials.resolve("does-not-exist"))
        self.assertIsNone(llm_credentials.resolve(""))
        self.assertIsNone(llm_credentials.resolve(None))

    def test_update_service_token_caches_it(self):
        cred_id = self._connect()
        expiry = time.time() + 900
        ok = llm_credentials.update_service_token(cred_id, "svc-tok", expiry)
        self.assertTrue(ok)
        record = llm_credentials.resolve(cred_id)
        self.assertEqual(record["service_token"], "svc-tok")
        self.assertEqual(record["service_token_expiry"], expiry)

    def test_update_service_token_on_unknown_id_is_noop(self):
        self.assertFalse(llm_credentials.update_service_token("nope", "x", 0))

    def test_describe_never_echoes_the_secret(self):
        cred_id = self._connect(token="super-secret-value")
        described = llm_credentials.describe(cred_id)
        self.assertEqual(described["connected"], True)
        self.assertNotIn("oauth_token", described)
        self.assertNotIn("service_token", described)
        self.assertNotIn("super-secret-value", json.dumps(described))

    def test_describe_unconnected_is_false(self):
        self.assertEqual(llm_credentials.describe("nope"), {"connected": False})

    def test_disconnect_then_resolve_fails_closed(self):
        """Acceptance criterion #5: disconnect removes the credential; a later lookup with the stale
        credential_id fails closed (None), not a stale/cached success."""
        cred_id = self._connect()
        self.assertIsNotNone(llm_credentials.resolve(cred_id))
        self.assertTrue(llm_credentials.disconnect(cred_id))
        self.assertIsNone(llm_credentials.resolve(cred_id))
        self.assertEqual(llm_credentials.describe(cred_id), {"connected": False})
        self._ids.remove(cred_id)  # already gone, nothing for tearDown to do

    def test_disconnect_unknown_id_is_false_not_an_error(self):
        self.assertFalse(llm_credentials.disconnect("never-existed"))
        self.assertFalse(llm_credentials.disconnect(""))

    def test_ram_only_never_touches_disk(self):
        """The whole point of this store: connect/resolve/update/describe/disconnect must NEVER call
        `open()` -- there is no llm_credentials.json, unlike llm_routes.json. Spy on builtins.open
        (not just check the filesystem afterwards, which could pass by accident on write buffering)
        to prove the full lifecycle genuinely does zero file I/O."""
        calls = []
        real_open = builtins.open

        def spy_open(*args, **kwargs):
            calls.append((args, kwargs))
            return real_open(*args, **kwargs)

        with mock.patch("builtins.open", spy_open):
            cred_id = llm_credentials.connect("secret-token", owner_uid="u1")
            llm_credentials.resolve(cred_id)
            llm_credentials.update_service_token(cred_id, "svc", time.time() + 100)
            llm_credentials.describe(cred_id)
            llm_credentials.count()
            llm_credentials.disconnect(cred_id)

        self.assertEqual(calls, [])


class GithubCopilotDirectProviderTests(unittest.TestCase):
    """4b scaffold: with the network exchange stubbed, chat() returns the right chat-style message
    shape and the error taxonomy maps 401/403/429 to distinct exception types."""

    def setUp(self):
        self._ids = []
        self._override_token = None

    def tearDown(self):
        config.set_llm_override(None)
        for cred_id in self._ids:
            llm_credentials.disconnect(cred_id)

    def _connect(self, token="real-oauth-token"):
        cred_id = llm_credentials.connect(token)
        self._ids.append(cred_id)
        return cred_id

    def _bind(self, credential_id):
        return config.set_llm_override(
            {"mode": "copilot_token", "provider": "github_copilot_direct",
             "credential_id": credential_id}
        )

    @staticmethod
    def _fake_open(token_body=None, chat_body=None, token_status=200, chat_status=200):
        """Stand-in for github_copilot_direct._open: routes on which URL is being fetched instead of
        hitting the network, so the scaffold is testable without a real Copilot endpoint."""
        def _open(req, connect_timeout, read_timeout):
            url = req.full_url if hasattr(req, "full_url") else req
            is_token_call = url == github_copilot_direct.GITHUB_COPILOT_TOKEN_URL
            status = token_status if is_token_call else chat_status
            body = (token_body if is_token_call else chat_body) or {}
            if status >= 400:
                raise urllib.error.HTTPError(
                    url, status, "error",
                    {"Content-Type": "application/json"},
                    io.BytesIO(json.dumps(body).encode("utf-8")),
                )
            payload = io.BytesIO(json.dumps(body).encode("utf-8"))

            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

                def read(self_inner):
                    return payload.read()

            return _Resp()
        return _open

    def test_chat_happy_path_returns_chat_style_message(self):
        cred_id = self._connect()
        fake_open = self._fake_open(
            token_body={"token": "svc-abc", "expires_at": time.time() + 3600},
            chat_body={"choices": [{"message": {"role": "assistant", "content": "hi there"}}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
        )
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                message = llm.chat([{"role": "user", "content": "hello"}])
        finally:
            config.reset_llm_override(otoken)

        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "hi there")
        self.assertEqual(message["_usage"]["input_tokens"], 5)
        self.assertEqual(message["_usage"]["output_tokens"], 3)
        self.assertEqual(message["_usage"]["total_tokens"], 8)

    def test_service_token_is_cached_across_calls(self):
        """Stage 1 (token exchange) should only fire once per still-valid service token."""
        cred_id = self._connect()
        exchange_calls = []
        real_exchange = github_copilot_direct._exchange_service_token

        def counting_exchange(oauth_token):
            exchange_calls.append(oauth_token)
            return real_exchange(oauth_token)

        fake_open = self._fake_open(
            token_body={"token": "svc-cached", "expires_at": time.time() + 3600},
            chat_body={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}},
        )
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open), \
                 mock.patch.object(github_copilot_direct, "_exchange_service_token", counting_exchange):
                llm.chat([{"role": "user", "content": "one"}])
                llm.chat([{"role": "user", "content": "two"}])
        finally:
            config.reset_llm_override(otoken)

        self.assertEqual(len(exchange_calls), 1)

    def test_401_maps_to_auth_error(self):
        cred_id = self._connect()
        fake_open = self._fake_open(token_body={"error": "bad creds"}, token_status=401)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(github_copilot_direct.CopilotAuthError):
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_403_maps_to_forbidden_error(self):
        cred_id = self._connect()
        fake_open = self._fake_open(token_body={"error": "no access"}, token_status=403)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(github_copilot_direct.CopilotForbiddenError):
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_429_maps_to_rate_limit_error(self):
        cred_id = self._connect()
        fake_open = self._fake_open(token_body={"error": "slow down"}, token_status=429)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(github_copilot_direct.CopilotRateLimitError):
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_401_error_message_never_contains_the_oauth_token(self):
        cred_id = self._connect(token="do-not-leak-this-token")
        fake_open = self._fake_open(token_body={"error": "bad creds"}, token_status=401)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(github_copilot_direct.CopilotAuthError) as caught:
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)
        self.assertNotIn("do-not-leak-this-token", str(caught.exception))

    def test_no_credential_id_in_context_raises_credential_error(self):
        otoken = config.set_llm_override({"provider": "github_copilot_direct"})  # no credential_id
        try:
            with self.assertRaises(github_copilot_direct.CredentialError):
                llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_stale_credential_id_fails_closed(self):
        """Acceptance criterion #5, provider layer: a disconnected credential_id must not silently
        proceed (e.g. by falling through to some other endpoint) -- it must raise."""
        cred_id = self._connect()
        self.assertTrue(llm_credentials.disconnect(cred_id))
        self._ids.remove(cred_id)
        otoken = self._bind(cred_id)
        try:
            with self.assertRaises(github_copilot_direct.CredentialError):
                llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)


class NoSecretLoggingTests(unittest.TestCase):
    """Acceptance criterion #4: after a token-mode chat, logs contain zero occurrences of the pasted
    token, the derived service token, or an `Authorization: Bearer` header value."""

    def test_stubbed_token_chat_never_logs_the_secret(self):
        raw_oauth_token = "ghu_totally_secret_oauth_value"
        service_token = "svc_totally_secret_service_value"
        cred_id = llm_credentials.connect(raw_oauth_token, owner_uid="u1")

        def fake_open(req, connect_timeout, read_timeout):
            is_token_call = req.full_url == github_copilot_direct.GITHUB_COPILOT_TOKEN_URL
            body = ({"token": service_token, "expires_at": time.time() + 3600} if is_token_call
                    else {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            payload = io.BytesIO(json.dumps(body).encode("utf-8"))

            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

                def read(self_inner):
                    return payload.read()

            return _Resp()

        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()

        otoken = config.set_llm_override(
            {"mode": "copilot_token", "provider": "github_copilot_direct", "credential_id": cred_id}
        )
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    message = llm.chat([{"role": "user", "content": "hello"}])
        finally:
            config.reset_llm_override(otoken)
            root_logger.removeHandler(handler)
            llm_credentials.disconnect(cred_id)

        self.assertEqual(message["content"], "hi")
        captured = log_stream.getvalue() + stdout_buf.getvalue() + stderr_buf.getvalue()
        self.assertNotIn(raw_oauth_token, captured)
        self.assertNotIn(service_token, captured)
        self.assertNotIn("Authorization: Bearer", captured)
        self.assertNotIn("token " + raw_oauth_token, captured)  # the stage-1 auth header shape


class ServerHelperFlagOffTests(unittest.TestCase):
    """Server-side wiring (6): with the flag off, `_describe_llm`/`_resolve_llm_override` must
    delegate straight to the pre-existing llm_routes calls -- no new lookups, no new keys."""

    def _handler(self):
        return webserver.Handler.__new__(webserver.Handler)

    def test_describe_llm_delegates_when_flag_off(self):
        handler = self._handler()
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", False), \
             mock.patch.object(llm_routes, "describe", return_value={"registered": False}) as spy:
            result = handler._describe_llm("sometoken")
        spy.assert_called_once_with("sometoken")
        self.assertEqual(result, {"registered": False})  # no "mode"/"token_mode_available" added

    def test_resolve_override_delegates_when_flag_off(self):
        handler = self._handler()
        tunnel_override = {"base_url": "http://127.0.0.1:24101/v1"}
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", False), \
             mock.patch.object(llm_routes, "resolve", return_value=tunnel_override) as spy:
            result = handler._resolve_llm_override("sometoken")
        spy.assert_called_once_with("sometoken")
        self.assertIs(result, tunnel_override)


class ServerHelperFlagOnTests(unittest.TestCase):
    """Server-side wiring (6): with the flag on, both helpers also check the credential store."""

    def setUp(self):
        self._patch = mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", True)
        self._patch.start()
        self._ids = []

    def tearDown(self):
        self._patch.stop()
        for cred_id in self._ids:
            llm_credentials.disconnect(cred_id)

    def _handler(self):
        return webserver.Handler.__new__(webserver.Handler)

    def _connect(self):
        cred_id = llm_credentials.connect("tok")
        self._ids.append(cred_id)
        return cred_id

    def test_resolve_override_prefers_tunnel_over_credential(self):
        """If a token happens to collide between the two stores, the tunnel registry wins (checked
        first) -- deterministic, and matches _resolve_llm_override's documented order."""
        handler = self._handler()
        cred_id = self._connect()
        tunnel_override = {"base_url": "http://127.0.0.1:24101/v1"}
        with mock.patch.object(llm_routes, "resolve", return_value=tunnel_override):
            result = handler._resolve_llm_override(cred_id)
        self.assertIs(result, tunnel_override)

    def test_resolve_override_selects_token_mode_when_connected(self):
        handler = self._handler()
        cred_id = self._connect()
        with mock.patch.object(llm_routes, "resolve", return_value=None):
            result = handler._resolve_llm_override(cred_id)
        self.assertEqual(result, {"mode": "copilot_token", "provider": "github_copilot_direct",
                                   "credential_id": cred_id})

    def test_resolve_override_unknown_token_falls_back_to_shared(self):
        handler = self._handler()
        with mock.patch.object(llm_routes, "resolve", return_value=None):
            result = handler._resolve_llm_override("never-connected")
        self.assertIsNone(result)

    def test_describe_llm_reports_token_mode(self):
        handler = self._handler()
        cred_id = self._connect()
        with mock.patch.object(llm_routes, "describe", return_value={"registered": False}):
            result = handler._describe_llm(cred_id)
        self.assertEqual(result["registered"], True)
        self.assertEqual(result["mode"], "copilot_token")
        self.assertNotIn("oauth_token", json.dumps(result))

    def test_describe_llm_shared_when_nothing_connected(self):
        handler = self._handler()
        with mock.patch.object(llm_routes, "describe", return_value={"registered": False}):
            result = handler._describe_llm("never-connected")
        self.assertEqual(result, {"registered": False, "mode": "shared", "token_mode_available": True})


class ServerEndpointHttpTests(unittest.TestCase):
    """End-to-end over real sockets (mirrors tests/test_retrieval_service.py's pattern): the actual
    POST /api/llm/connect-token, POST /api/llm/disconnect-token, and GET /api/llm/me routes."""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), webserver.Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.srv.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _post(self, path, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.base + path)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_routes_404_when_flag_off(self):
        # The 404 body is plain text ("not found"), same as any other unknown path -- not JSON, so
        # this doesn't go through _post's JSON decoding.
        data = json.dumps({"token": "x"}).encode("utf-8")
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", False):
            for path in ("/api/llm/connect-token", "/api/llm/disconnect-token"):
                req = urllib.request.Request(self.base + path, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()

    def test_me_shape_unchanged_when_flag_off(self):
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", False):
            status, body = self._get("/api/llm/me")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"registered": False})  # exact -- no new keys when off

    def test_connect_me_disconnect_lifecycle_over_http(self):
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", True):
            status, body = self._post("/api/llm/connect-token", {"token": "pasted-token-value"})
            self.assertEqual(status, 200)
            cred_id = body["credential_id"]
            self.assertTrue(cred_id)

            status, me = self._get("/api/llm/me", {"X-SDLC-User-Token": cred_id})
            self.assertEqual(status, 200)
            self.assertTrue(me["registered"])
            self.assertEqual(me["mode"], "copilot_token")
            self.assertNotIn("pasted-token-value", json.dumps(me))

            status, body = self._post("/api/llm/disconnect-token", {"credential_id": cred_id})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])

            status, me = self._get("/api/llm/me", {"X-SDLC-User-Token": cred_id})
            self.assertEqual(status, 200)
            self.assertFalse(me["registered"])

    def test_connect_token_blank_is_400(self):
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", True):
            status, body = self._post("/api/llm/connect-token", {"token": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()

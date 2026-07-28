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

from webapp import agent, config, llm_routes, llm, llm_credentials
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


class SelectedModelOverrideTests(unittest.TestCase):
    """Dynamic model selector: LLM_SELECTED_MODEL resolves the same override-then-env way as the
    other LLM_* fields, and llm_default_model() ignores any active override (mirrors
    llm_default_base_url/llm_default_provider)."""

    def tearDown(self):
        config.set_llm_override(None)

    def test_selected_model_override_wins_then_falls_back(self):
        token = config.set_llm_override({"selected_model": "gpt-9000"})
        try:
            self.assertEqual(config.LLM_SELECTED_MODEL, "gpt-9000")
        finally:
            config.reset_llm_override(token)
        self.assertEqual(config.LLM_SELECTED_MODEL, "")  # unset -> env default (blank)

    def test_selected_model_is_isolated_from_model(self):
        """selected_model and model are distinct override keys -- setting one must not change the
        other (model still feeds the provider payload; selected_model is what status endpoints
        report)."""
        token = config.set_llm_override({"model": "provider-model", "selected_model": "ui-model"})
        try:
            self.assertEqual(config.LLM_MODEL, "provider-model")
            self.assertEqual(config.LLM_SELECTED_MODEL, "ui-model")
        finally:
            config.reset_llm_override(token)

    def test_llm_default_model_ignores_active_override(self):
        default = config.llm_default_model()
        token = config.set_llm_override({"model": "something-else", "selected_model": "something-else"})
        try:
            self.assertEqual(config.llm_default_model(), default)
        finally:
            config.reset_llm_override(token)


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

    def test_register_preserves_provider_when_updating_model_only(self):
        """A partial update (e.g. the model-switch flow re-registering with the same token) must not
        silently wipe a previously-stored provider -- only label already had this fallback; provider
        needs it too now that select-model re-registers with provider="" to mean 'unchanged'."""
        first = llm_routes.register("http://127.0.0.1:24101/v1", model="m1", provider="openai_chat",
                                     label="alice")
        second = llm_routes.register("http://127.0.0.1:24101/v1", model="m2", token=first["token"])
        self.assertEqual(second["model"], "m2")
        self.assertEqual(second["label"], "alice")  # pre-existing fallback, unaffected

    def test_resolve_carries_the_registered_provider_into_the_override(self):
        """A registered provider must actually reach the request-level override -- previously
        register() stored `provider` but resolve() never surfaced it, so a tunnel registered with a
        non-default provider silently kept using the server's env-default provider for every probe,
        model list, and chat."""
        record = llm_routes.register("http://127.0.0.1:24101/v1", model="m1", provider="openai_chat")
        self.assertEqual(llm_routes.resolve(record["token"])["provider"], "openai_chat")

    def test_resolve_omits_provider_when_never_set(self):
        record = llm_routes.register("http://127.0.0.1:24101/v1", model="m1")
        self.assertNotIn("provider", llm_routes.resolve(record["token"]))

    def test_validate_base_url_is_public(self):
        self.assertEqual(llm_routes.validate_base_url("http://127.0.0.1:24101/v1/"),
                          "http://127.0.0.1:24101/v1")
        with self.assertRaises(ValueError):
            llm_routes.validate_base_url("http://evil.example.com/v1")
        # validation only -- never registers anything
        self.assertEqual(llm_routes.describe("anything"), {"registered": False})

    @staticmethod
    def _json_response(payload):
        body = json.dumps(payload).encode("utf-8")

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return body

        return _Resp()

    def test_list_remote_models_parses_openai_style_response(self):
        resp = self._json_response({"data": [{"id": "m1"}, {"id": "m2", "label": "Model Two"}]})
        with mock.patch.object(llm_routes, "_open_models_request", return_value=resp):
            listing = llm_routes.list_remote_models("http://127.0.0.1:24101/v1")
        self.assertEqual(listing, {"models": [{"id": "m1", "label": "m1"},
                                               {"id": "m2", "label": "Model Two"}]})

    def test_list_remote_models_parses_bare_models_key_response(self):
        """Some endpoints answer {"models": [...]} directly instead of the OpenAI-style
        {"data": [...]} envelope -- both must parse, not silently return an empty list."""
        resp = self._json_response({"models": [{"id": "m1"}]})
        with mock.patch.object(llm_routes, "_open_models_request", return_value=resp):
            listing = llm_routes.list_remote_models("http://127.0.0.1:24101/v1")
        self.assertEqual(listing, {"models": [{"id": "m1", "label": "m1"}]})

    def test_list_remote_models_never_registers_a_route(self):
        resp = self._json_response({"data": []})
        with mock.patch.object(llm_routes, "_open_models_request", return_value=resp):
            llm_routes.list_remote_models("http://127.0.0.1:24101/v1")
        self.assertEqual(llm_routes.describe("anything"), {"registered": False})
        self.assertFalse(os.path.exists(config.LLM_ROUTES_STORE))

    def test_list_remote_models_rejects_non_loopback(self):
        with self.assertRaises(ValueError):
            llm_routes.list_remote_models("http://evil.example.com/v1")

    def test_list_remote_models_network_failure_is_sanitized_runtime_error(self):
        with mock.patch.object(llm_routes, "_open_models_request",
                                side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(RuntimeError) as caught:
                llm_routes.list_remote_models("http://127.0.0.1:24101/v1")
        self.assertNotIn("connection refused", str(caught.exception))


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


class LlmModelsFacadeTests(unittest.TestCase):
    """llm.models() -- the model-listing half of the provider contract (llm.models()/llm.chat()).
    Forwards to the current provider's own models() when it has one; degrades to a single-entry
    list built from the configured model when it doesn't (external repo must not hard-crash just
    because the internal-owned provider files haven't grown models() yet)."""

    def tearDown(self):
        config.set_llm_override(None)

    def test_mock_mode_returns_single_self_consistent_model(self):
        with mock.patch.object(config, "LLM_MOCK", True):
            listing = llm.models()
        self.assertEqual(listing["models"], [{"id": listing["default_model"], "label": listing["default_model"]}])

    def test_forwards_to_provider_models_when_present(self):
        fake_listing = {"models": [{"id": "m1", "label": "Model One"}], "default_model": "m1"}
        with mock.patch.object(copilot_responses, "models", create=True, return_value=fake_listing):
            self.assertEqual(llm.models(), fake_listing)

    def test_falls_back_to_single_entry_when_provider_lacks_models(self):
        # None of the real provider files implement models() in this repo yet -- llm.py must
        # degrade gracefully (not AttributeError) rather than assuming every provider has grown one.
        self.assertFalse(hasattr(copilot_responses, "models"))
        listing = llm.models()
        self.assertEqual(listing, {"models": [{"id": config.LLM_MODEL, "label": config.LLM_MODEL}],
                                    "default_model": config.LLM_MODEL})

    def test_fallback_respects_the_active_model_override(self):
        token = config.set_llm_override({"model": "overridden-model"})
        try:
            listing = llm.models()
        finally:
            config.reset_llm_override(token)
        self.assertEqual(listing["default_model"], "overridden-model")


class LlmErrorNormalizationTests(unittest.TestCase):
    """llm.py must be the ONLY place a `llm_providers/*` exception type is imported/caught --
    everything downstream (server.py) depends on the small llm.Llm*Error vocabulary instead, so a
    provider being refactored, split, or lazily imported can never break webapp startup or force
    server.py to couple to provider internals (see the "错误归一化" section of the internal review)."""

    def test_facade_maps_provider_rate_limit_and_retry_after(self):
        fake_error = github_copilot_direct.CopilotRateLimitError("slow down")
        fake_error.retry_after = 30

        def raising_chat(messages, tools=None, temperature=0):
            raise fake_error

        with mock.patch.object(copilot_responses, "chat", side_effect=raising_chat):
            with self.assertRaises(llm.LlmRateLimitError) as caught:
                llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.retry_after, 30)

    def test_facade_maps_auth_and_forbidden_errors(self):
        for provider_error, neutral_error in (
            (github_copilot_direct.CopilotAuthError("nope"), llm.LlmAuthError),
            (github_copilot_direct.CopilotForbiddenError("nope"), llm.LlmForbiddenError),
            (github_copilot_direct.CredentialError("credential is gone"), llm.LlmAuthError),
        ):
            def raising_chat(messages, tools=None, temperature=0, _error=provider_error):
                raise _error

            with mock.patch.object(copilot_responses, "chat", side_effect=raising_chat):
                with self.assertRaises(neutral_error):
                    llm.chat([{"role": "user", "content": "hi"}])

    def test_facade_maps_models_listing_errors_too(self):
        def raising_models():
            raise github_copilot_direct.CopilotAuthError("nope")

        with mock.patch.object(copilot_responses, "models", create=True, side_effect=raising_models):
            with self.assertRaises(llm.LlmAuthError):
                llm.models()

    def test_unrecognized_provider_errors_pass_through_unchanged(self):
        """Not every provider failure is auth/forbidden/rate-limit -- e.g. a plain network error --
        those must reach the caller as-is, not get coerced into one of the three neutral types."""
        with mock.patch.object(copilot_responses, "chat", side_effect=RuntimeError("network blip")):
            with self.assertRaises(RuntimeError) as caught:
                llm.chat([{"role": "user", "content": "hi"}])
        self.assertNotIsInstance(caught.exception, (llm.LlmAuthError, llm.LlmForbiddenError,
                                                       llm.LlmRateLimitError))

    def test_chat_stream_blocking_fallback_is_also_normalized(self):
        """chat_stream()'s own blocking fallback used to call provider.chat() directly, bypassing
        whatever normalization chat() did -- must go through the same normalized path."""
        with mock.patch.object(config, "LLM_STREAM", False), \
             mock.patch.object(copilot_responses, "chat",
                                side_effect=github_copilot_direct.CopilotAuthError("nope")):
            with self.assertRaises(llm.LlmAuthError):
                list(llm.chat_stream([{"role": "user", "content": "hi"}]))

    def test_server_does_not_import_or_reference_provider_exception_types(self):
        """Acceptance: server.py depends only on llm.Llm*Error, never a llm_providers/* exception
        class. `"github_copilot_direct"` legitimately appears as a plain STRING VALUE (the provider
        selector, e.g. `"provider": "github_copilot_direct"`) -- that's just naming which provider to
        route to, the same string a tunnel's `provider` field can already hold, not a code coupling
        -- and mentioning a provider module by name in a comment is fine too. What must never exist
        is an actual `import` of a `llm_providers.*` module -- parsed via `ast` (not a text grep, so
        comments/docstrings/string literals can't produce a false positive)."""
        import ast
        server_path = os.path.join(os.path.dirname(webserver.__file__), "server.py")
        with open(server_path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=server_path)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported_names.update(alias.name.split(".")[0] for alias in node.names)
        self.assertNotIn("github_copilot_direct", imported_names)
        self.assertNotIn("copilot_responses", imported_names)
        self.assertFalse(hasattr(webserver, "github_copilot_direct"))
        self.assertFalse(hasattr(webserver, "copilot_responses"))


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
        self.assertIsNone(record["selected_model"])  # unset until a probe confirms one

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

    def test_credential_ids_carry_the_ct_prefix(self):
        cred_id = self._connect()
        self.assertTrue(cred_id.startswith("ct_"))
        self.assertTrue(llm_credentials.is_credential_id(cred_id))

    def test_is_credential_id_rejects_non_matching_shapes(self):
        self.assertFalse(llm_credentials.is_credential_id("plain-tunnel-token"))
        self.assertFalse(llm_credentials.is_credential_id("ct_tooshort"))
        self.assertFalse(llm_credentials.is_credential_id("ct_" + "g" * 32))  # not hex
        self.assertFalse(llm_credentials.is_credential_id(None))
        self.assertFalse(llm_credentials.is_credential_id(123))

    def test_is_credential_id_is_shape_only_not_a_store_lookup(self):
        """A well-formed but never-connected id still reports True -- this is what lets server.py
        keep a dead credential pinned to the direct provider (fail closed) instead of falling back
        to shared just because the store lookup came back empty."""
        self.assertTrue(llm_credentials.is_credential_id("ct_" + "0" * 32))

    def test_is_valid_model_id_true_false(self):
        self.assertTrue(llm_credentials.is_valid_model_id("gpt-4o"))
        self.assertTrue(llm_credentials.is_valid_model_id("openai/gpt-4o"))
        self.assertFalse(llm_credentials.is_valid_model_id(""))
        self.assertFalse(llm_credentials.is_valid_model_id("bad model id!!"))

    def test_resolve_with_owner_uid_enforces_ownership(self):
        """Bob cannot resolve Alice's credential -- a mismatched owner_uid fails closed to None,
        the same as an unknown id, so there's no way to tell the two cases apart."""
        cred_id = self._connect(owner_uid="alice")
        self.assertIsNotNone(llm_credentials.resolve(cred_id, owner_uid="alice"))
        self.assertIsNone(llm_credentials.resolve(cred_id, owner_uid="bob"))
        self.assertIsNotNone(llm_credentials.resolve(cred_id))  # owner_uid=None -> no filter, unchanged

    def test_update_service_token_with_owner_uid_enforces_ownership(self):
        cred_id = self._connect(owner_uid="alice")
        self.assertFalse(llm_credentials.update_service_token(cred_id, "svc", 0, owner_uid="bob"))
        self.assertIsNone(llm_credentials.resolve(cred_id)["service_token"])  # bob's write never landed
        self.assertTrue(llm_credentials.update_service_token(cred_id, "svc", 0, owner_uid="alice"))
        self.assertEqual(llm_credentials.resolve(cred_id)["service_token"], "svc")

    def test_describe_with_owner_uid_enforces_ownership(self):
        cred_id = self._connect(owner_uid="alice")
        self.assertEqual(llm_credentials.describe(cred_id, owner_uid="bob"), {"connected": False})
        self.assertTrue(llm_credentials.describe(cred_id, owner_uid="alice")["connected"])

    def test_bob_cannot_disconnect_alices_credential(self):
        """Acceptance: the isolation bug where any browser holding a credential_id could disconnect
        it is closed -- disconnect is now owner_uid-gated exactly like resolve/update_service_token."""
        cred_id = self._connect(owner_uid="alice")
        self.assertFalse(llm_credentials.disconnect(cred_id, owner_uid="bob"))
        self.assertIsNotNone(llm_credentials.resolve(cred_id, owner_uid="alice"))  # still connected
        self.assertTrue(llm_credentials.disconnect(cred_id, owner_uid="alice"))
        self._ids.remove(cred_id)

    def test_set_selected_model_persists_and_describe_reports_it(self):
        cred_id = self._connect(owner_uid="alice")
        self.assertTrue(llm_credentials.set_selected_model(cred_id, "gpt-4o", owner_uid="alice"))
        self.assertEqual(llm_credentials.resolve(cred_id)["selected_model"], "gpt-4o")
        self.assertEqual(llm_credentials.describe(cred_id)["selected_model"], "gpt-4o")

    def test_set_selected_model_rejects_bad_model_id(self):
        cred_id = self._connect()
        with self.assertRaises(ValueError):
            llm_credentials.set_selected_model(cred_id, "", owner_uid="u1")
        with self.assertRaises(ValueError):
            llm_credentials.set_selected_model(cred_id, "bad model id!!", owner_uid="u1")
        self.assertIsNone(llm_credentials.resolve(cred_id)["selected_model"])  # rejected, unchanged

    def test_set_selected_model_enforces_ownership(self):
        """Acceptance: one tester can never repoint another tester's credential at a different
        model, even knowing its credential_id."""
        cred_id = self._connect(owner_uid="alice")
        with self.assertRaises(PermissionError):
            llm_credentials.set_selected_model(cred_id, "gpt-4o", owner_uid="mallory")
        self.assertIsNone(llm_credentials.resolve(cred_id)["selected_model"])  # unchanged

    def test_set_selected_model_on_unknown_id_is_false(self):
        self.assertFalse(llm_credentials.set_selected_model("nope", "gpt-4o", owner_uid=""))

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
            llm_credentials.set_selected_model(cred_id, "gpt-4o", owner_uid="u1")
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
        # Match the owner-bound contract the internal provider enforces on the box. Keeping the
        # fixture explicit stops this public routing suite from accidentally depending on an older
        # provider revision that ignored LLM_CREDENTIAL_OWNER_UID.
        cred_id = llm_credentials.connect(token, owner_uid="u1")
        self._ids.append(cred_id)
        return cred_id

    def _bind(self, credential_id):
        return config.set_llm_override(
            {"mode": "copilot_token", "provider": "github_copilot_direct",
             "credential_id": credential_id, "credential_owner_uid": "u1",
             "model": "gpt-5.4", "selected_model": "gpt-5.4"}
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
        """llm.chat() normalizes the raw provider exception to the provider-neutral llm.LlmAuthError
        -- callers (server.py) must never need to import github_copilot_direct to catch this."""
        cred_id = self._connect()
        fake_open = self._fake_open(token_body={"error": "bad creds"}, token_status=401)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(llm.LlmAuthError):
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_403_maps_to_forbidden_error(self):
        cred_id = self._connect()
        fake_open = self._fake_open(token_body={"error": "no access"}, token_status=403)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(llm.LlmForbiddenError):
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_429_maps_to_rate_limit_error(self):
        cred_id = self._connect()
        fake_open = self._fake_open(token_body={"error": "slow down"}, token_status=429)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(llm.LlmRateLimitError) as caught:
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)
        # the underlying CopilotRateLimitError doesn't (yet) carry retry_after -- must degrade to
        # None, not blow up.
        self.assertIsNone(caught.exception.retry_after)

    def test_401_error_message_never_contains_the_oauth_token(self):
        cred_id = self._connect(token="do-not-leak-this-token")
        fake_open = self._fake_open(token_body={"error": "bad creds"}, token_status=401)
        otoken = self._bind(cred_id)
        try:
            with mock.patch.object(github_copilot_direct, "_open", fake_open):
                with self.assertRaises(llm.LlmAuthError) as caught:
                    llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)
        self.assertNotIn("do-not-leak-this-token", str(caught.exception))

    def test_no_credential_id_in_context_maps_to_neutral_auth_error(self):
        otoken = config.set_llm_override({"provider": "github_copilot_direct"})  # no credential_id
        try:
            with self.assertRaises(llm.LlmAuthError):
                llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

    def test_stale_credential_id_fails_closed_as_neutral_auth_error(self):
        """Acceptance criterion #5, provider layer: a disconnected credential_id must not silently
        proceed (e.g. by falling through to some other endpoint) -- it must raise."""
        cred_id = self._connect()
        self.assertTrue(llm_credentials.disconnect(cred_id))
        self._ids.remove(cred_id)
        otoken = self._bind(cred_id)
        try:
            with self.assertRaises(llm.LlmAuthError):
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
            {"mode": "copilot_token", "provider": "github_copilot_direct",
             "credential_id": cred_id, "credential_owner_uid": "u1",
             "model": "gpt-5.4", "selected_model": "gpt-5.4"}
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


class FrontendModelSelectorSourceTests(unittest.TestCase):
    """Static checks on webapp/static/index.html's inline JS -- for a race-condition class of bug
    that's easiest (and, for statement ORDER within a function body, only practically possible) to
    pin at the source level rather than by driving a real browser."""

    @staticmethod
    def _index_html_source():
        path = os.path.join(os.path.dirname(webserver.__file__), "static", "index.html")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _function_body(source, name):
        """Extract one `function <name>(...) { ... }`'s body via brace matching (a regex alone
        can't handle the nested braces a real function body has)."""
        marker = "function " + name + "("
        start = source.index(marker)
        brace_start = source.index("{", start)
        depth = 0
        for i in range(brace_start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return source[brace_start:i + 1]
        raise AssertionError(f"unbalanced braces in function {name}")

    def test_stale_model_listing_cannot_mutate_dropdown_before_revision_check(self):
        """Acceptance: loadTokenModelOptions() must check `revision !== llmStatusRevision` BEFORE
        calling populateModelSelect() -- otherwise a slower, now-superseded model-list response
        writes to the dropdown before it discovers it's stale."""
        body = self._function_body(self._index_html_source(), "loadTokenModelOptions")
        guard_position = body.index("revision !== llmStatusRevision")
        dom_write_position = body.index("populateModelSelect")
        self.assertLess(guard_position, dom_write_position)

    def test_frontend_preserves_structured_llm_errors_and_refreshes_on_auth_failure(self):
        source = self._index_html_source()
        fetch_start = source.index("async function fetchJson(")
        fetch_end = source.index("async function refreshSessions", fetch_start)
        fetch_body = source[fetch_start:fetch_end]
        for field in ("error.status", "error.code", "error.retryAfter", "error.retryable",
                      "error.reconnectRequired"):
            self.assertIn(field, fetch_body)
        self.assertIn("refreshLlmStatusAfterAuthFailure(error)", source)
        self.assertIn("d.reconnect_required", source)

    def test_connect_reuses_its_returned_models_listing(self):
        body = self._function_body(self._index_html_source(), "connectLlm")
        self.assertIn("refreshLlmStatus(tokenListing)", body)
        refresh_body = self._function_body(self._index_html_source(), "refreshLlmStatus")
        self.assertLess(refresh_body.index("if (tokenListing)"),
                        refresh_body.index("await loadTokenModelOptions"))

    def test_streaming_429_uses_structured_code_and_snake_case_retry_after(self):
        """NDJSON stream errors do not have an HTTP status or fetchJson's camelCase conversion;
        the live askStream path must still turn the server's structured 429 event into a useful
        retry/model-switch message rather than rendering only its generic sanitized error."""
        source = self._index_html_source()
        describe_body = self._function_body(source, "describeLlmError")
        stream_body = self._function_body(source, "askStream")
        self.assertIn("error.code === 'copilot_rate_limit'", describe_body)
        self.assertIn("error.retryAfter ?? error.retry_after", describe_body)
        self.assertIn("describeLlmError(event)", stream_body)


class PickDefaultModelTests(unittest.TestCase):
    """server._pick_default_model -- the model to try first out of an llm.models() listing."""

    def test_prefers_default_model_when_it_is_actually_listed(self):
        listing = {"models": [{"id": "m1"}, {"id": "m2"}], "default_model": "m2"}
        self.assertEqual(webserver._pick_default_model(listing), "m2")

    def test_ignores_a_default_model_not_in_the_listing(self):
        """A provider's default_model and its models list can disagree (e.g. a deploy changed the
        default before the list caught up) -- picking an unlisted "ghost" model would make every
        probe against it fail for a reason that has nothing to do with the user's actual endpoint."""
        listing = {"models": [{"id": "m1"}], "default_model": "ghost"}
        self.assertEqual(webserver._pick_default_model(listing), "m1")

    def test_falls_back_to_first_listed_when_no_default_model(self):
        listing = {"models": [{"id": "m1"}, {"id": "m2"}], "default_model": ""}
        self.assertEqual(webserver._pick_default_model(listing), "m1")

    def test_empty_listing_yields_blank(self):
        self.assertEqual(webserver._pick_default_model({}), "")
        self.assertEqual(webserver._pick_default_model(None), "")

    def test_ignores_malformed_entries(self):
        listing = {"models": ["not-a-dict", {"id": ""}, {"id": "m1"}], "default_model": ""}
        self.assertEqual(webserver._pick_default_model(listing), "m1")

    def test_token_initial_model_prefers_auto_before_provider_default(self):
        listing = {"models": [{"id": "m1"}, {"id": "auto"}], "default_model": "m1"}
        self.assertEqual(webserver._pick_token_initial_model(listing), "auto")


class ProbeLlmTests(unittest.TestCase):
    """server._probe_llm -- must validate the response shape, not just "no exception raised"."""

    def tearDown(self):
        config.set_llm_override(None)

    def test_succeeds_on_a_well_formed_assistant_message(self):
        with mock.patch.object(llm, "chat", return_value={"role": "assistant", "content": "OK"}) as spy:
            webserver._probe_llm({})
        spy.assert_called_once()
        # the probe message itself matches what the review asked for (a real instruction, not "ping")
        self.assertEqual(spy.call_args.args[0], [{"role": "user", "content": "Reply with exactly OK."}])

    def test_passes_through_the_tools_argument(self):
        with mock.patch.object(llm, "chat", return_value={"role": "assistant", "content": "OK"}) as spy:
            webserver._probe_llm({}, tools=agent.tools.TOOLS)
        self.assertEqual(spy.call_args.kwargs.get("tools"), agent.tools.TOOLS)

    def test_none_response_is_a_failed_probe(self):
        """A provider returning None (e.g. a bug, or an unexpected empty body) must not be treated as
        a successful connection just because llm.chat() didn't raise."""
        with mock.patch.object(llm, "chat", return_value=None):
            with self.assertRaises(RuntimeError):
                webserver._probe_llm({})

    def test_malformed_dict_response_is_a_failed_probe(self):
        with mock.patch.object(llm, "chat", return_value={"not": "a chat message"}):
            with self.assertRaises(RuntimeError):
                webserver._probe_llm({})

    def test_non_assistant_role_is_a_failed_probe(self):
        with mock.patch.object(llm, "chat", return_value={"role": "system", "content": "OK"}):
            with self.assertRaises(RuntimeError):
                webserver._probe_llm({})


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

    def _handler(self, uid=""):
        handler = webserver.Handler.__new__(webserver.Handler)
        handler._uid = uid
        return handler

    def _connect(self, owner_uid=""):
        cred_id = llm_credentials.connect("tok", owner_uid=owner_uid)
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
                                   "credential_id": cred_id, "credential_owner_uid": ""})

    def test_resolve_override_includes_confirmed_model_once_selected(self):
        """Once connect-token/select-model has probed and confirmed a model, every subsequent chat
        turn for this credential must actually use it -- not silently fall back to the env default."""
        handler = self._handler()
        cred_id = self._connect()
        llm_credentials.set_selected_model(cred_id, "gpt-4o", owner_uid="")
        with mock.patch.object(llm_routes, "resolve", return_value=None):
            result = handler._resolve_llm_override(cred_id)
        self.assertEqual(result, {"mode": "copilot_token", "provider": "github_copilot_direct",
                                   "credential_id": cred_id, "credential_owner_uid": "",
                                   "model": "gpt-4o", "selected_model": "gpt-4o"})

    def test_resolve_override_unknown_token_falls_back_to_shared(self):
        handler = self._handler()
        with mock.patch.object(llm_routes, "resolve", return_value=None):
            result = handler._resolve_llm_override("never-connected")
        self.assertIsNone(result)

    def test_resolve_override_fails_closed_to_direct_provider_for_a_dead_credential_id(self):
        """Acceptance: a credential-shaped token that no longer resolves (disconnected, or never
        belonged to this caller) must still be pinned to the direct provider -- NOT released back to
        None/shared -- so the provider's own credential check is what fails the request."""
        handler = self._handler()
        dead_id = "ct_" + "0" * 32  # well-formed shape, never actually connected
        with mock.patch.object(llm_routes, "resolve", return_value=None):
            result = handler._resolve_llm_override(dead_id)
        self.assertEqual(result, {"mode": "copilot_token", "provider": "github_copilot_direct",
                                   "credential_id": dead_id, "credential_owner_uid": ""})

    def test_resolve_override_does_not_leak_a_credential_belonging_to_another_owner(self):
        """A credential that exists but belongs to a DIFFERENT owner must resolve exactly like a dead
        one above (still pinned to the direct provider, no model carried over) -- never the other
        owner's confirmed model."""
        handler = self._handler(uid="mallory")
        cred_id = self._connect(owner_uid="alice")
        llm_credentials.set_selected_model(cred_id, "gpt-4o", owner_uid="alice")
        with mock.patch.object(llm_routes, "resolve", return_value=None):
            result = handler._resolve_llm_override(cred_id)
        self.assertEqual(result, {"mode": "copilot_token", "provider": "github_copilot_direct",
                                   "credential_id": cred_id, "credential_owner_uid": "mallory"})
        self.assertNotIn("model", result)

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
        self.assertEqual(result, {"registered": False, "mode": "shared", "token_mode_available": True,
                                   "model": config.llm_default_model()})


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
        # connect-token now runs a real llm.models()/llm.chat() probe before reporting Connected
        # (dynamic model selector) -- mock the facade so this stays a pure HTTP/store test, no real
        # Copilot endpoint involved. Ownership is now enforced (owner_uid=self._uid on every
        # credential lookup), so this must be the SAME "browser" (sdlc_uid cookie) across every call
        # -- otherwise even its own just-created credential would look like someone else's.
        browser = _Browser(self.base)
        listing = {"models": [{"id": "m1", "label": "Model One"}], "default_model": "m1"}
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", True), \
             mock.patch.object(llm, "models", return_value=listing), \
             mock.patch.object(llm, "chat", return_value={"role": "assistant", "content": "pong"}):
            status, body = browser.post("/api/llm/connect-token", {"token": "pasted-token-value"})
            self.assertEqual(status, 200)
            cred_id = body["credential_id"]
            self.assertTrue(cred_id)
            self.assertEqual(body["model"], "m1")

            status, me = browser.get("/api/llm/me", {"X-SDLC-User-Token": cred_id})
            self.assertEqual(status, 200)
            self.assertTrue(me["registered"])
            self.assertEqual(me["mode"], "copilot_token")
            self.assertEqual(me["model"], "m1")
            self.assertNotIn("pasted-token-value", json.dumps(me))

            status, body = browser.post("/api/llm/disconnect-token", {"credential_id": cred_id},
                                         {"X-SDLC-User-Token": cred_id})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])

            status, me = browser.get("/api/llm/me", {"X-SDLC-User-Token": cred_id})
            self.assertEqual(status, 200)
            self.assertFalse(me["registered"])

    def test_connect_token_blank_is_400(self):
        with mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", True):
            status, body = self._post("/api/llm/connect-token", {"token": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", body)


def _raw_post(base, path, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), e.headers.get("Set-Cookie")


def _raw_get(base, path, headers=None):
    req = urllib.request.Request(base + path)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), e.headers.get("Set-Cookie")


class _Browser:
    """Stand-in for one real browser talking to the test server: remembers the `sdlc_uid` cookie
    across calls (like a browser would), so ownership checks that depend on 'this is the same
    browser that connected the credential' behave correctly across separate HTTP requests. A second
    `_Browser` instance with its own (never-shared) cookie simulates a different user/browser.

    Also remembers `user_token` (the credential_id or tunnel token from a successful connect/
    register) and sends it as `X-SDLC-User-Token` on every later call, mirroring the real frontend's
    `authHeaders()` (index.html) -- set it explicitly after a successful connect/register."""

    def __init__(self, base):
        self.base = base
        self.cookie = None
        self.user_token = None

    def _headers(self, extra):
        headers = {}
        if self.user_token:
            headers["X-SDLC-User-Token"] = self.user_token
        if self.cookie:
            headers["Cookie"] = self.cookie
        headers.update(extra or {})  # an explicit per-call header (e.g. impersonation) wins
        return headers

    def post(self, path, payload, headers=None):
        status, body, set_cookie = _raw_post(self.base, path, payload, self._headers(headers))
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        return status, body

    def get(self, path, headers=None):
        status, body, set_cookie = _raw_get(self.base, path, self._headers(headers))
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        return status, body

    @property
    def uid(self):
        """This browser's `self._uid` on the server (parsed from its sdlc_uid cookie) -- for tests
        that need to reconstruct a bare Handler acting as "this same browser"."""
        if not self.cookie:
            return ""
        _, _, value = self.cookie.partition("=")
        return value


class DynamicModelSelectorTests(unittest.TestCase):
    """The Copilot dynamic-model-selector endpoints: probe-gated POST /api/llm/connect-token, GET
    /api/llm/models, POST /api/llm/select-model, POST /api/llm/tunnel-models, and probe-gated POST
    /api/llm/register. Per the external test boundary, this uses ONLY a mocked `llm` facade
    (llm.chat/llm.models monkeypatched) -- never a real Copilot endpoint or network call."""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), webserver.Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.srv.server_address[:2]
        self.base = f"http://{host}:{port}"
        self._tmp = tempfile.TemporaryDirectory()
        store = os.path.join(self._tmp.name, "llm_routes.json")
        self._patches = [
            mock.patch.object(config, "LLM_ROUTES_STORE", store),
            mock.patch.object(config, "LLM_ALLOW_NONLOOPBACK", False),
            mock.patch.object(config, "LLM_TOKEN_MODE_ENABLED", True),
        ]
        for p in self._patches:
            p.start()
        self.browser = _Browser(self.base)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _ok_chat():
        return mock.patch.object(llm, "chat", return_value={"role": "assistant", "content": "pong"})

    # ---- connect-token: probe success/failure lifecycle -----------------------------------------

    def test_connect_token_returns_listing_and_selects_initial_model_without_a_chat_probe(self):
        listing = {"models": [{"id": "m1", "label": "Model One"}, {"id": "m2", "label": "Model Two"}],
                   "default_model": "m2"}
        with mock.patch.object(llm, "models", return_value=listing), \
             mock.patch.object(llm, "chat") as chat_spy:
            status, body = self.browser.post("/api/llm/connect-token", {"token": "tok-a"})
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["model"], "m2")
        self.assertEqual(body["selected_model"], "m2")
        self.assertFalse(body["model_verified"])
        self.assertEqual(body["models"], listing["models"])
        self.assertEqual(body["default_model"], "m2")
        self.assertEqual(llm_credentials.resolve(body["credential_id"])["selected_model"], "m2")
        chat_spy.assert_not_called()

    def test_connect_token_probe_failure_cleans_up_credential(self):
        before = llm_credentials.count()
        with mock.patch.object(llm, "models", return_value={"models": [], "default_model": ""}), \
             mock.patch.object(llm, "chat", side_effect=RuntimeError("token rejected (401)")):
            status, body = self.browser.post("/api/llm/connect-token", {"token": "tok-bad"})
        self.assertEqual(status, 400)
        self.assertEqual(llm_credentials.count(), before)  # nothing left dangling in RAM
        self.assertNotIn("token rejected", json.dumps(body))  # sanitized, no upstream detail leaked

    def test_two_credentials_have_independent_selected_models(self):
        """Acceptance: multi-user credential/model isolation."""
        alice, bob = _Browser(self.base), _Browser(self.base)
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, alice_body = alice.post("/api/llm/connect-token", {"token": "alice-token"})
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m9"}], "default_model": "m9"}), \
             self._ok_chat():
            _, bob_body = bob.post("/api/llm/connect-token", {"token": "bob-token"})

        self.assertNotEqual(alice_body["credential_id"], bob_body["credential_id"])
        self.assertEqual(alice_body["model"], "m1")
        self.assertEqual(bob_body["model"], "m9")
        self.assertEqual(llm_credentials.resolve(alice_body["credential_id"])["selected_model"], "m1")
        self.assertEqual(llm_credentials.resolve(bob_body["credential_id"])["selected_model"], "m9")

    # ---- stale credential -> reconnect_required, never silently "shared" ------------------------

    def test_stale_credential_status_requires_reconnect_but_chat_stays_fail_closed(self):
        """A credential-shaped token that no longer resolves (RAM cleared, disconnected elsewhere,
        etc.) must report reconnect_required=True, mode=copilot_token -- NOT mode=shared, which
        would wrongly imply nothing is wrong. Chat routing independently stays pinned to the direct
        provider (fail closed), matching what /api/llm/me now says instead of contradicting it."""
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]
        llm_credentials.disconnect(cred_id, owner_uid=self.browser.uid)  # simulate RAM cleared

        status, me = self.browser.get("/api/llm/me", {"X-SDLC-User-Token": cred_id})
        self.assertEqual(status, 200)
        self.assertEqual(me, {"registered": False, "mode": "copilot_token", "label": "Copilot token",
                               "token_mode_available": True, "model": "", "reconnect_required": True})

        handler = webserver.Handler.__new__(webserver.Handler)
        handler._uid = self.browser.uid
        override = handler._resolve_llm_override(cred_id)
        self.assertEqual(override["provider"], "github_copilot_direct")  # still fail-closed, not None

        # Run a chat through the real facade/provider seam, not a pre-raised facade mock: the
        # provider sees the dead credential and raises CredentialError, llm.py normalizes it, and
        # server.py returns 401/reconnect instead of its generic 500 path.
        self.browser.user_token = cred_id
        with mock.patch.object(agent, "answer",
                               side_effect=lambda *_args, **_kwargs: llm.chat(
                                   [{"role": "user", "content": "hello"}])):
            status, chat_body = self.browser.post("/api/chat", {"question": "hello"})
        self.assertEqual(status, 401)
        self.assertTrue(chat_body["reconnect_required"])

    def test_auth_disconnect_status_requires_reconnect(self):
        """A live chat call that gets rejected (401) mid-session must disconnect the credential --
        so the NEXT /api/llm/me check reports reconnect_required, not a stale "Connected"."""
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]
        self.browser.user_token = cred_id  # so /api/chat actually binds to THIS credential

        with mock.patch.object(agent, "answer", side_effect=llm.LlmAuthError("token rejected")):
            status, chat_body = self.browser.post("/api/chat", {"question": "hello"})
        self.assertEqual(status, 401)
        self.assertTrue(chat_body.get("reconnect_required"))

        status, me = self.browser.get("/api/llm/me")
        self.assertEqual(status, 200)
        self.assertTrue(me.get("reconnect_required"))
        self.assertFalse(me["registered"])

    def test_rate_limit_during_chat_reports_retry_after(self):
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]
        self.browser.user_token = cred_id

        rate_limit_error = llm.LlmRateLimitError("slow down", retry_after=15)
        with mock.patch.object(agent, "answer", side_effect=rate_limit_error):
            status, chat_body = self.browser.post("/api/chat", {"question": "hello"})
        self.assertEqual(status, 429)
        self.assertEqual(chat_body.get("retry_after"), 15)
        self.assertEqual(chat_body.get("code"), "copilot_rate_limit")
        self.assertTrue(chat_body.get("retryable"))
        # a rate limit is not an auth failure -- the credential must stay connected
        self.assertIsNotNone(llm_credentials.resolve(cred_id, owner_uid=self.browser.uid))

    # ---- Token -> Tunnel credential migration ----------------------------------------------------

    def test_token_to_tunnel_gets_new_route_token_and_disconnects_old_credential(self):
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]
        self.browser.user_token = cred_id

        with self._ok_chat():
            status, record = self.browser.post(
                "/api/llm/register",
                {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "previous_credential_id": cred_id})
        self.assertEqual(status, 200)
        self.assertNotEqual(record["token"], cred_id)
        self.assertFalse(llm_credentials.is_credential_id(record["token"]))
        self.assertTrue(llm_routes.resolve(record["token"]))
        # the old Token credential is gone -- retired only AFTER the new route persisted
        self.assertIsNone(llm_credentials.resolve(cred_id))

    def test_token_to_tunnel_probe_failure_keeps_old_credential(self):
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]
        self.browser.user_token = cred_id

        with mock.patch.object(llm, "chat", side_effect=RuntimeError("connection refused")):
            status, _ = self.browser.post(
                "/api/llm/register",
                {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "previous_credential_id": cred_id})
        self.assertEqual(status, 400)
        # neither side effect happened: no tunnel persisted, old credential untouched
        self.assertFalse(os.path.exists(config.LLM_ROUTES_STORE))
        self.assertIsNotNone(llm_credentials.resolve(cred_id, owner_uid=self.browser.uid))

    def test_register_never_persists_a_credential_shaped_route_token(self):
        """Defense in depth: even if a caller (an old frontend, or a manual API call) puts a
        credential-shaped id directly in `token` instead of `previous_credential_id`, it must never
        become the new route's identity -- it's redirected to "credential to retire" instead."""
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]

        with self._ok_chat():
            status, record = self.browser.post(
                "/api/llm/register",
                {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "token": cred_id})
        self.assertEqual(status, 200)
        self.assertNotEqual(record["token"], cred_id)
        self.assertFalse(llm_credentials.is_credential_id(record["token"]))
        # treated as if it had arrived via previous_credential_id: retired after success
        self.assertIsNone(llm_credentials.resolve(cred_id))

    # ---- select-model: switch commit/rollback --------------------------------------------------

    def _connect(self, browser, models_list, default_model):
        listing = {"models": models_list, "default_model": default_model}
        with mock.patch.object(llm, "models", return_value=listing), self._ok_chat():
            _, body = browser.post("/api/llm/connect-token", {"token": "tok"})
        browser.user_token = body["credential_id"]  # mirrors the frontend storing it for later calls
        return body["credential_id"]

    def test_select_model_success_commits_new_model(self):
        cred_id = self._connect(self.browser, [{"id": "m1"}], "m1")
        listing = {"models": [{"id": "m1"}, {"id": "m2"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing), \
             mock.patch.object(llm, "chat") as chat_spy:
            status, body = self.browser.post("/api/llm/select-model", {"model": "m2"})
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "m2")
        self.assertEqual(body["selected_model"], "m2")
        self.assertFalse(body["model_verified"])
        self.assertEqual(llm_credentials.resolve(cred_id)["selected_model"], "m2")
        # Token mode: the models() listing IS the real authenticated check -- no separate chat()
        # probe, so switching models never spends a Copilot completion.
        chat_spy.assert_not_called()

    def test_select_model_failure_keeps_old_model(self):
        """For a TOKEN-mode credential, a failed switch can only come from the models() listing
        itself -- there is no separate chat probe (see test_select_model_success_commits_new_model)."""
        cred_id = self._connect(self.browser, [{"id": "m1"}], "m1")
        with mock.patch.object(llm, "models", side_effect=RuntimeError("boom")):
            status, body = self.browser.post("/api/llm/select-model", {"model": "m2"})
        self.assertEqual(status, 502)
        self.assertEqual(llm_credentials.resolve(cred_id)["selected_model"], "m1")  # unchanged

    def test_select_model_for_tunnel_failure_keeps_old_model(self):
        """Tunnel switches DO still re-probe with a real llm.chat() call (an arbitrary user-supplied
        endpoint, same as register()) -- a failed probe must leave the tunnel's previously-confirmed
        model untouched."""
        with self._ok_chat():
            status, record = self.browser.post(
                "/api/llm/register", {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "label": "alice"})
        self.assertEqual(status, 200)
        self.browser.user_token = record["token"]
        listing = {"models": [{"id": "m1"}, {"id": "m2"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing), \
             mock.patch.object(llm, "chat", side_effect=RuntimeError("boom")):
            status, body = self.browser.post("/api/llm/select-model", {"model": "m2"})
        self.assertEqual(status, 400)
        self.assertEqual(llm_routes.resolve(record["token"])["model"], "m1")  # unchanged

    def test_select_model_rejects_model_not_in_current_list(self):
        cred_id = self._connect(self.browser, [{"id": "m1"}], "m1")
        listing = {"models": [{"id": "m1"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing):
            status, body = self.browser.post("/api/llm/select-model", {"model": "ghost-model"})
        self.assertEqual(status, 400)
        self.assertEqual(llm_credentials.resolve(cred_id)["selected_model"], "m1")

    def test_select_model_without_a_connection_is_400(self):
        status, body = self.browser.post("/api/llm/select-model", {"model": "m1"})
        self.assertEqual(status, 400)

    def test_select_model_blank_model_is_400(self):
        self._connect(self.browser, [{"id": "m1"}], "m1")
        status, body = self.browser.post("/api/llm/select-model", {"model": ""})
        self.assertEqual(status, 400)

    def test_select_model_cannot_be_hijacked_by_a_different_owner(self):
        """A stranger who learns the credential_id gets the SAME 400 as "nothing connected" -- the
        fail-closed owner_uid check in llm_credentials.resolve() makes an owned-by-someone-else
        credential indistinguishable from a nonexistent one, so there's no oracle for "does this
        credential_id exist"."""
        cred_id = self._connect(self.browser, [{"id": "m1"}], "m1")
        stranger = _Browser(self.base)  # a different browser -- no shared cookie, different uid
        listing = {"models": [{"id": "m1"}, {"id": "m2"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing):
            status, body = stranger.post("/api/llm/select-model", {"model": "m2"},
                                          {"X-SDLC-User-Token": cred_id})
        self.assertEqual(status, 400)
        self.assertEqual(llm_credentials.resolve(cred_id)["selected_model"], "m1")

    def test_select_model_for_tunnel_updates_the_route_not_a_credential(self):
        with self._ok_chat():
            status, record = self.browser.post(
                "/api/llm/register", {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "label": "alice"})
        self.assertEqual(status, 200)
        self.browser.user_token = record["token"]  # mirrors the frontend storing it for later calls
        listing = {"models": [{"id": "m1"}, {"id": "m2"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing), self._ok_chat():
            status, body = self.browser.post("/api/llm/select-model", {"model": "m2"})
        self.assertEqual(status, 200)
        updated = llm_routes.resolve(record["token"])
        self.assertEqual(updated["model"], "m2")
        # provider-neutral (no credential store involvement) -- label survives the partial update
        self.assertEqual(llm_routes.describe(record["token"])["label"], "alice")

    # ---- tunnel: probe-gated register + read-only model listing ---------------------------------

    def test_register_requires_successful_probe(self):
        with mock.patch.object(llm, "chat", side_effect=RuntimeError("connection refused")):
            status, body = self.browser.post(
                "/api/llm/register", {"base_url": "http://127.0.0.1:24101/v1", "model": "m1"})
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(config.LLM_ROUTES_STORE))  # nothing persisted

    def test_register_persists_after_successful_probe(self):
        with self._ok_chat():
            status, body = self.browser.post(
                "/api/llm/register", {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "label": "alice"})
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "m1")
        self.assertTrue(llm_routes.resolve(body["token"]))

    def test_register_uses_the_requested_provider_for_the_probe_and_persists_it(self):
        """The provider field must actually reach the probe -- previously register() stored
        `provider` but resolve() never surfaced it, so a tunnel registered with a non-default
        provider silently kept probing/chatting through the server's env-default provider."""
        seen_providers = []

        def fake_chat(messages, tools=None, temperature=0):
            seen_providers.append(config.LLM_PROVIDER)
            return {"role": "assistant", "content": "OK"}

        with mock.patch.object(llm, "chat", side_effect=fake_chat):
            status, record = self.browser.post(
                "/api/llm/register",
                {"base_url": "http://127.0.0.1:24101/v1", "model": "m1", "provider": "openai_chat"})
        self.assertEqual(status, 200)
        self.assertIn("openai_chat", seen_providers)
        self.assertEqual(llm_routes.resolve(record["token"])["provider"], "openai_chat")

    def test_register_probe_uses_the_real_mdc_tool_schema(self):
        """Acceptance: the tunnel register probe must exercise the same tools shape real chat turns
        use, not a bare no-tools call that could pass even where the real tool-calling request shape
        fails."""
        seen_tools = []

        def fake_chat(messages, tools=None, temperature=0):
            seen_tools.append(tools)
            return {"role": "assistant", "content": "OK"}

        with mock.patch.object(llm, "chat", side_effect=fake_chat):
            self.browser.post("/api/llm/register",
                               {"base_url": "http://127.0.0.1:24101/v1", "model": "m1"})
        self.assertTrue(seen_tools)
        self.assertEqual(seen_tools[-1], agent.tools.TOOLS)

    def test_connect_token_never_calls_chat(self):
        """Acceptance: Token Connect must not spend a real inference request -- llm.models() (a
        real authenticated call in its own right) is sufficient confirmation on its own."""
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             mock.patch.object(llm, "chat") as chat_spy:
            status, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        self.assertEqual(status, 200)
        chat_spy.assert_not_called()

    def test_tunnel_models_lists_without_registering(self):
        body_bytes = json.dumps({"data": [{"id": "m1"}, {"id": "m2", "label": "Model Two"}]}).encode("utf-8")

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return body_bytes

        with mock.patch.object(llm_routes, "_open_models_request", return_value=_Resp()):
            status, body = self.browser.post("/api/llm/tunnel-models", {"base_url": "http://127.0.0.1:24101/v1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["models"], [{"id": "m1", "label": "m1"}, {"id": "m2", "label": "Model Two"}])
        self.assertFalse(os.path.exists(config.LLM_ROUTES_STORE))

    # ---- GET /api/llm/models: works for shared/tunnel/token alike --------------------------------

    def test_get_models_reflects_the_bound_provider(self):
        cred_id = self._connect(self.browser, [{"id": "m1"}, {"id": "m3"}], "m1")
        listing = {"models": [{"id": "m1"}, {"id": "m3"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing):
            status, body = self.browser.get("/api/llm/models")
        self.assertEqual(status, 200)
        self.assertEqual(body, listing)

    def test_models_auth_failure_disconnects_token_credential(self):
        cred_id = self._connect(self.browser, [{"id": "m1"}], "m1")
        with mock.patch.object(llm, "models", side_effect=llm.LlmAuthError("expired")):
            status, body = self.browser.get("/api/llm/models")
        self.assertEqual(status, 401)
        self.assertTrue(body["reconnect_required"])
        self.assertIsNone(llm_credentials.resolve(cred_id, owner_uid=self.browser.uid))
        status, me = self.browser.get("/api/llm/me")
        self.assertEqual(status, 200)
        self.assertTrue(me["reconnect_required"])

    def test_select_auth_failure_disconnects_token_credential(self):
        cred_id = self._connect(self.browser, [{"id": "m1"}], "m1")
        with mock.patch.object(llm, "models", side_effect=llm.LlmForbiddenError("forbidden")):
            status, body = self.browser.post("/api/llm/select-model", {"model": "m1"})
        self.assertEqual(status, 403)
        self.assertTrue(body["reconnect_required"])
        self.assertIsNone(llm_credentials.resolve(cred_id, owner_uid=self.browser.uid))

    # ---- sanitized errors: never leak secrets/URLs/upstream bodies -------------------------------

    def test_connect_token_error_never_leaks_upstream_detail(self):
        secret_detail = "Authorization: Bearer sk-supersecret at http://internal.example.com/copilot"
        with mock.patch.object(llm, "models", return_value={"models": [], "default_model": ""}), \
             mock.patch.object(llm, "chat", side_effect=RuntimeError(secret_detail)):
            status, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        self.assertEqual(status, 400)
        dumped = json.dumps(body)
        self.assertNotIn("sk-supersecret", dumped)
        self.assertNotIn("internal.example.com", dumped)
        self.assertNotIn("Authorization", dumped)

    def test_register_error_never_leaks_upstream_detail(self):
        secret_detail = "copilot-api unreachable at http://127.0.0.1:24101/v1: [Errno 111] refused"
        with mock.patch.object(llm, "chat", side_effect=RuntimeError(secret_detail)):
            status, body = self.browser.post("/api/llm/register", {"base_url": "http://127.0.0.1:24101/v1"})
        self.assertEqual(status, 400)
        self.assertNotIn("127.0.0.1:24101", json.dumps(body))

    # ---- real facade -> internal provider contract (not just llm.models()/llm.chat() mocked) -----

    def test_real_facade_to_provider_contract_stub(self):
        """This is NOT another llm.models()/llm.chat() facade mock -- it stands in for the REAL
        (internal-owned, not present in this repo) github_copilot_direct.chat()'s actual call
        pattern, as described by the internal review: it reads config.LLM_CREDENTIAL_OWNER_UID (not
        just LLM_CREDENTIAL_ID), and calls llm_credentials.resolve(credential_id, owner_uid=...),
        .update_service_token(..., owner_uid=...), and .is_valid_model_id(). Only ONE attribute on
        the real (otherwise untouched) provider module is monkeypatched for this one test.

        Connect-token itself no longer calls llm.chat() (see test_connect_token_never_calls_chat),
        so this exercises the contract the way a REAL chat turn would: bind the just-connected
        credential's override (exactly what _resolve_llm_override hands agent.answer()) and call
        llm.chat() directly -- llm.py's facade, config.py's override plumbing, and
        llm_credentials.py are all exercised for real."""

        def contract_stub_chat(messages, tools=None, temperature=0):
            credential_id = config.LLM_CREDENTIAL_ID
            owner_uid = config.LLM_CREDENTIAL_OWNER_UID
            record = llm_credentials.resolve(credential_id, owner_uid=owner_uid)
            if record is None:
                raise github_copilot_direct.CredentialError("no active credential for this session")
            if not llm_credentials.is_valid_model_id(config.LLM_MODEL or "stub-model"):
                raise RuntimeError("invalid model id")
            llm_credentials.update_service_token(credential_id, "stub-service-token",
                                                  time.time() + 900, owner_uid=owner_uid)
            return {"role": "assistant", "content": "contract ok"}

        listing = {"models": [{"id": "m1"}], "default_model": "m1"}
        with mock.patch.object(llm, "models", return_value=listing):
            status, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        self.assertEqual(status, 200)
        cred_id = body["credential_id"]

        handler = webserver.Handler.__new__(webserver.Handler)
        handler._uid = self.browser.uid
        override = handler._resolve_llm_override(cred_id)

        otoken = config.set_llm_override(override)
        try:
            with mock.patch.object(github_copilot_direct, "chat", side_effect=contract_stub_chat):
                message = llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)

        self.assertEqual(message["content"], "contract ok")
        self.assertEqual(llm_credentials.resolve(cred_id)["service_token"], "stub-service-token")

    def test_bob_cannot_chat_through_alices_credential(self):
        """Acceptance: multi-user isolation end-to-end through _resolve_llm_override -- Bob sending
        Alice's credential_id as his X-SDLC-User-Token must not be able to bind a request to it."""
        alice = self.browser
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = alice.post("/api/llm/connect-token", {"token": "alice-secret"})
        cred_id = body["credential_id"]

        bob = webserver.Handler.__new__(webserver.Handler)
        bob._uid = "bob-uid"  # a different browser/session -- never shared alice's sdlc_uid cookie
        override = bob._resolve_llm_override(cred_id)
        # still pinned to the direct provider (fail closed -- see next test), but WITHOUT alice's
        # confirmed model, and tagged with BOB's uid, not alice's
        self.assertEqual(override["credential_owner_uid"], "bob-uid")
        self.assertNotIn("model", override)

    def test_disconnect_then_stale_credential_fails_closed_as_neutral_auth_not_shared(self):
        """Acceptance: after disconnect, a request that still carries the old credential_id must
        FAIL -- through the real facade's neutral LlmAuthError (from the provider's CredentialError)
        -- never silently fall back to the shared/default LLM just because the credential lookup
        came back empty."""
        with mock.patch.object(llm, "models", return_value={"models": [{"id": "m1"}], "default_model": "m1"}), \
             self._ok_chat():
            _, body = self.browser.post("/api/llm/connect-token", {"token": "tok"})
        cred_id = body["credential_id"]

        status, _ = self.browser.post("/api/llm/disconnect-token", {"credential_id": cred_id})
        self.assertEqual(status, 200)

        handler = webserver.Handler.__new__(webserver.Handler)
        handler._uid = self.browser.uid
        override = handler._resolve_llm_override(cred_id)
        # still pinned to the direct provider -- NOT None/shared, despite the dead credential
        self.assertEqual(override["provider"], "github_copilot_direct")
        self.assertEqual(override["credential_id"], cred_id)
        self.assertNotIn("model", override)

        otoken = config.set_llm_override(override)
        try:
            with self.assertRaises(llm.LlmAuthError):
                llm.chat([{"role": "user", "content": "hi"}])
        finally:
            config.reset_llm_override(otoken)


if __name__ == "__main__":
    unittest.main()

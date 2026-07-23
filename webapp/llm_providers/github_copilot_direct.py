"""Direct GitHub Copilot provider -- internal-beta paste-token mode (`SDLC_LLM_TOKEN_MODE`).

THROWAWAY scaffold (see docs/specs/copilot-token-direct-mode.md), removed before GA. This file is
buildable and unit-testable here (stdlib only, exchange function is monkeypatchable) but the REAL
network specifics -- exact token-exchange URL/headers/field-names, the Copilot chat endpoint's exact
request/response shape, and enterprise proxy/CA config for server egress -- can only be confirmed on
the box. Every place that matters is marked `# CODEX-VERIFY`; see spec §8 "Internal Codex".

Two-stage flow:
  1. Exchange the user's pasted `.copilot_token` (an OAuth/Copilot credential, held in RAM by
     `webapp/llm_credentials.py`) for a short-lived Copilot "service token". Cached in RAM against
     the credential_id and re-exchanged once it's near expiry.
  2. Call the Copilot chat endpoint with that service token and convert the response to the SAME
     chat-style message shape `copilot_responses.py` produces (role/content/tool_calls + private
     `_usage`), so `agent.py` cannot tell providers apart.

All endpoints come from env (nothing box-specific baked into source). Explicit, independently
configurable connect and read timeouts on every request (stdlib's `urlopen(timeout=...)` only
exposes ONE timeout for the whole socket lifetime; `_open` below re-arms the socket's timeout right
after connect so a slow response can't hang past the read timeout even though the connect succeeded
quickly -- see `_open`/`_connection_factory`).

Never logs: the pasted token, the derived service token, or any `Authorization` header. Error
messages below intentionally include only status codes / truncated response bodies, never request
headers or the credential.
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.request

from .. import config, llm_credentials
from . import sanitize_messages

# CODEX-VERIFY: confirm these are the real GitHub/Copilot endpoints for this enterprise tenant --
# hostnames, paths, and whether they differ per org/deployment. These defaults are placeholders
# (the public github.com Copilot shapes), not verified against the box's actual endpoint.
GITHUB_COPILOT_TOKEN_URL = os.environ.get(
    "GITHUB_COPILOT_TOKEN_URL", "https://api.github.com/copilot_internal/v2/token"
)
GITHUB_COPILOT_API_URL = os.environ.get(
    "GITHUB_COPILOT_API_URL", "https://api.githubcopilot.com/chat/completions"
)

GITHUB_COPILOT_CONNECT_TIMEOUT = float(os.environ.get("GITHUB_COPILOT_CONNECT_TIMEOUT", "10"))
GITHUB_COPILOT_READ_TIMEOUT = float(os.environ.get("GITHUB_COPILOT_READ_TIMEOUT", "60"))

# Re-exchange the cached service token this many seconds before its reported expiry, so a chat call
# never races an expiry that lands mid-request.
_EXPIRY_SKEW_SECONDS = 30


# ---- distinct error types (§4b: "distinct 401/403/429/proxy/cert errors") ----------------------

class CopilotAuthError(RuntimeError):
    """401 from Copilot -- the pasted token is invalid, revoked, or expired. Never includes the
    token itself, only the status and a truncated response body."""


class CopilotForbiddenError(RuntimeError):
    """403 from Copilot -- token is valid but lacks Copilot access/entitlement."""


class CopilotRateLimitError(RuntimeError):
    """429 from Copilot -- rate limited; caller may retry later."""


class CopilotProxyError(RuntimeError):
    """Network/proxy failure reaching a Copilot endpoint (e.g. enterprise egress proxy not
    configured or blocking this host).
    # CODEX-VERIFY: wire the real enterprise proxy config for server egress on the box (this
    # scaffold makes no proxy calls of its own -- it relies on stdlib env/system proxy handling)."""


class CopilotCertError(RuntimeError):
    """TLS/certificate verification failure reaching a Copilot endpoint.
    # CODEX-VERIFY: install/point at the enterprise CA bundle on the box (e.g. SSL_CERT_FILE /
    # REQUESTS_CA_BUNDLE-equivalent for urllib) if the enterprise egress path re-signs TLS."""


class CredentialError(RuntimeError):
    """No usable credential for this request (never connected, or disconnected). Deliberately
    raised rather than silently falling back to the shared/default provider or endpoint -- token
    mode must fail closed, never spend someone else's quota or leak into the wrong endpoint."""


def _raise_for_http_error(e, context):
    detail = ""
    try:
        detail = e.read().decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001 -- best-effort detail only, never fatal
        pass
    # CODEX-VERIFY: confirm Copilot's real error body shape per status code; `detail` is just the
    # raw truncated body here, not parsed against a confirmed schema.
    if e.code == 401:
        raise CopilotAuthError(f"{context}: token rejected (401). {detail}") from None
    if e.code == 403:
        raise CopilotForbiddenError(
            f"{context}: forbidden (403) -- token lacks Copilot access. {detail}"
        ) from None
    if e.code == 429:
        raise CopilotRateLimitError(f"{context}: rate limited (429). {detail}") from None
    raise RuntimeError(f"{context}: HTTP {e.code}. {detail}") from None


def _raise_for_url_error(e, context):
    reason = str(getattr(e, "reason", e))
    # CODEX-VERIFY: this is a best-effort string classifier, not verified against the box's actual
    # proxy/CA failure modes (corporate MITM proxy, custom CA bundle path, etc). Replace with a
    # precise check (e.g. catching ssl.SSLCertVerificationError specifically) once known.
    lowered = reason.lower()
    if "certificate" in lowered or "ssl" in lowered:
        raise CopilotCertError(f"{context}: certificate verification failed -- {reason}") from None
    if "proxy" in lowered or "refused" in lowered or "timed out" in lowered or "timeout" in lowered:
        raise CopilotProxyError(f"{context}: network/proxy failure -- {reason}") from None
    raise RuntimeError(f"{context}: unreachable -- {reason}") from None


# ---- independently-configurable connect vs read timeouts ---------------------------------------

def _connection_factory(base_cls, read_timeout):
    """HTTPConnection/HTTPSConnection subclass that applies a distinct READ timeout to the socket
    immediately after connect() succeeds. `http.client`'s `timeout=` constructor arg (which urllib
    forwards as the CONNECT timeout) only bounds the TCP handshake; without this, that same timeout
    would silently also bound every subsequent send/recv, so a slow-but-connected endpoint couldn't
    be given a longer read allowance than the connect allowance (or vice versa)."""
    class _Connection(base_cls):
        def connect(self):
            super().connect()
            if self.sock is not None:
                self.sock.settimeout(read_timeout)
    return _Connection


class _TimeoutHTTPHandler(urllib.request.HTTPHandler):
    """`HTTPHandler` whose connection class is swappable -- the stdlib base class hardcodes
    `http.client.HTTPConnection` inside `http_open` itself (there is no `self.http_class` hook to
    override), so this exists purely to route through our read-timeout-aware connection class."""

    def __init__(self, connection_cls):
        super().__init__()
        self._connection_cls = connection_cls

    def http_open(self, req):
        return self.do_open(self._connection_cls, req)


class _TimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS counterpart of `_TimeoutHTTPHandler`. Uses the default SSL context (system CA store) --
    # CODEX-VERIFY: if the enterprise egress path re-signs TLS, pass the enterprise CA bundle as
    # `context=ssl.create_default_context(cafile=...)` here."""

    def __init__(self, connection_cls):
        super().__init__()
        self._connection_cls = connection_cls

    def https_open(self, req):
        return self.do_open(self._connection_cls, req, context=self._context,
                             check_hostname=self._check_hostname)


def _open(req, connect_timeout, read_timeout):
    """`urllib.request.urlopen`-equivalent with independently configurable connect/read timeouts.

    Raises the same `urllib.error.HTTPError`/`URLError` types `urlopen` would, so callers keep using
    the familiar except clauses."""
    http_handler = _TimeoutHTTPHandler(_connection_factory(http.client.HTTPConnection, read_timeout))
    https_handler = _TimeoutHTTPSHandler(_connection_factory(http.client.HTTPSConnection, read_timeout))
    opener = urllib.request.build_opener(http_handler, https_handler)
    return opener.open(req, timeout=connect_timeout)


# ---- stage 1: pasted token -> short-lived service token -----------------------------------------

def _exchange_service_token(oauth_token):
    """Stage 1: pasted `.copilot_token` -> (service_token, expiry_epoch_seconds).

    # CODEX-VERIFY: confirm the real request method/headers/field-names GitHub's token-exchange
    # endpoint expects (this GET + `Authorization: token <t>` shape is a plausible placeholder,
    # modelled on the public Copilot CLI flow, NOT verified against this box's actual endpoint), and
    # confirm the real response field names/units below (`token`, `expires_at` are guesses)."""
    req = urllib.request.Request(GITHUB_COPILOT_TOKEN_URL, method="GET")
    req.add_header("Authorization", f"token {oauth_token}")  # CODEX-VERIFY exact scheme/header name
    req.add_header("Accept", "application/json")
    try:
        with _open(req, GITHUB_COPILOT_CONNECT_TIMEOUT, GITHUB_COPILOT_READ_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _raise_for_http_error(e, "Copilot token exchange")  # always raises
    except urllib.error.URLError as e:
        _raise_for_url_error(e, "Copilot token exchange")  # always raises
    except OSError as e:
        # A READ-phase timeout (waiting on the response after the request was sent) surfaces as a
        # raw TimeoutError/socket.timeout, not urllib.error.URLError -- stdlib's do_open() only
        # wraps failures during the send phase. Route it through the same classifier.
        _raise_for_url_error(e, "Copilot token exchange")  # always raises

    service_token = body.get("token")  # CODEX-VERIFY real field name
    expires_at = body.get("expires_at")  # CODEX-VERIFY real field name + units (assumed epoch secs)
    if not service_token:
        # Deliberately does not include `body` -- an unexpected response could echo the request back.
        raise RuntimeError("Copilot token exchange: response had no service token")
    try:
        expiry = float(expires_at) if expires_at is not None else time.time() + 1500
    except (TypeError, ValueError):
        expiry = time.time() + 1500
    return service_token, expiry


def _service_token_for(credential_id):
    """RAM-cached service token for this credential; re-exchanges when missing or near expiry.
    Raises CredentialError (fail closed) if the credential_id is unknown/disconnected."""
    record = llm_credentials.resolve(credential_id)
    if record is None:
        raise CredentialError(
            "no active Copilot token credential for this session -- "
            "connect via POST /api/llm/connect-token"
        )

    now = time.time()
    expiry = record.get("service_token_expiry")
    if record.get("service_token") and expiry and now < (expiry - _EXPIRY_SKEW_SECONDS):
        return record["service_token"]

    service_token, new_expiry = _exchange_service_token(record["oauth_token"])
    llm_credentials.update_service_token(credential_id, service_token, new_expiry)
    return service_token


# ---- stage 2: chat -------------------------------------------------------------------------------

def _to_chat_payload(messages, tools, temperature):
    payload = {
        "model": config.LLM_MODEL,
        "messages": sanitize_messages(messages),
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _from_chat_completion(body):
    """Copilot chat body -> chat-style assistant message (+ private `_usage`), matching the shape
    `copilot_responses._from_responses` / `openai_chat.chat` both produce.
    # CODEX-VERIFY: confirms this against the REAL Copilot chat endpoint's response shape -- this
    # assumes an OpenAI-Chat-Completions-compatible `choices[0].message` + `usage`, not verified."""
    choices = body.get("choices") or [{}]
    first = choices[0] if choices else {}
    message = dict((first or {}).get("message") or {})
    message.setdefault("role", "assistant")
    message.setdefault("content", None)
    usage = body.get("usage") or {}
    message["_usage"] = {
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
        "total_tokens": usage.get("total_tokens", 0),
    }
    return message


def chat(messages, tools=None, temperature=0):
    credential_id = config.LLM_CREDENTIAL_ID
    if not credential_id:
        raise CredentialError("no Copilot token credential bound to this request")

    service_token = _service_token_for(credential_id)

    payload = _to_chat_payload(messages, tools, temperature)
    req = urllib.request.Request(
        GITHUB_COPILOT_API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {service_token}")  # CODEX-VERIFY exact scheme Copilot expects
    # CODEX-VERIFY: the real Copilot chat endpoint typically requires extra headers (observed on the
    # public API: an editor/integration identifier). Confirm and add them here once known -- do not
    # add box-specific values without confirming on the box first.

    try:
        with _open(req, GITHUB_COPILOT_CONNECT_TIMEOUT, GITHUB_COPILOT_READ_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _raise_for_http_error(e, "Copilot chat")  # always raises
    except urllib.error.URLError as e:
        _raise_for_url_error(e, "Copilot chat")  # always raises
    except OSError as e:
        # See the matching comment in _exchange_service_token: a read-phase timeout surfaces as a
        # raw TimeoutError, not URLError.
        _raise_for_url_error(e, "Copilot chat")  # always raises

    return _from_chat_completion(body)

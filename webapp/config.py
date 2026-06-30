"""Web app config — all via env vars so Codex/ops can set them without code edits."""
import os

# ---- model (Codex sets these) ----
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.5")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))
LLM_MOCK = os.environ.get("LLM_MOCK", "") not in ("", "0", "false", "False")

# ---- assistant behaviour ----
SYSTEM_PROMPT = os.environ.get(
    "SDLC_SYSTEM_PROMPT", os.path.join(os.getcwd(), "prompts", "qa-system-prompt.md")
)
MAX_TOOL_ITERS = int(os.environ.get("SDLC_MAX_TOOL_ITERS", "8"))
TOOL_RESULT_CAP = int(os.environ.get("SDLC_TOOL_RESULT_CAP", "12000"))

# ---- server ----
HOST = os.environ.get("SDLC_HOST", "127.0.0.1")
PORT = int(os.environ.get("SDLC_PORT", "8765"))

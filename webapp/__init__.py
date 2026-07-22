"""
Thin web Q&A app for the MDC assistant.

Pipeline:  browser -> server.py -> agent.py (tool loop) -> llm.py (the model)
                                              agent.py -> tools.py -> retriever/

Standard library only (urllib for the model call, http.server for the web).
Runs with ZERO installs. Read-only.

The ONLY place that talks to the model is `llm.py` — Codex wires GPT-5.5 there.
Everything else is complete and model-agnostic.
"""

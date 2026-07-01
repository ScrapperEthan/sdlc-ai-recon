"""JSON-backed chat sessions for internal testing."""
import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from . import config
from . import llm_usage

_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _title_from_question(question, limit=48):
    compact = " ".join((question or "").split())
    if not compact:
        return "New session"
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "..."


def _ensure_parent_dir():
    parent = os.path.dirname(config.SESSION_STORE)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_unlocked():
    if not os.path.exists(config.SESSION_STORE):
        return {"sessions": []}

    with open(config.SESSION_STORE, encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise RuntimeError(f"Invalid session store format in {config.SESSION_STORE}")
    return data


def _save_unlocked(data):
    _ensure_parent_dir()
    temp_path = config.SESSION_STORE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, config.SESSION_STORE)


def _find_session(data, session_id):
    for session in data["sessions"]:
        if session.get("id") == session_id:
            return session
    return None


def _session_summary(session):
    messages = session.get("messages") or []
    first_user_message = next(
        (message.get("content", "") for message in messages if message.get("role") == "user"),
        "",
    )
    return {
        "id": session.get("id", ""),
        "title": session.get("title") or "New session",
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(messages),
        "preview": first_user_message,
    }


def _session_detail(session):
    detail = _session_summary(session)
    detail["messages"] = [
        {
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
            "created_at": message.get("created_at"),
            "tool_trace": deepcopy(message.get("tool_trace") or []),
            "usage": deepcopy(message.get("usage")) if message.get("usage") else None,
            "citations": deepcopy(message.get("citations")) if message.get("citations") else None,
        }
        for message in session.get("messages") or []
    ]
    return detail


def list_sessions():
    with _LOCK:
        data = _load_unlocked()
        sessions = sorted(
            data["sessions"],
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        return [_session_summary(session) for session in sessions]


def create_session(title="New session"):
    now = _now()
    session = {
        "id": uuid.uuid4().hex,
        "title": title.strip() or "New session",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    with _LOCK:
        data = _load_unlocked()
        data["sessions"].append(session)
        _save_unlocked(data)
    return _session_detail(session)


def get_session(session_id):
    with _LOCK:
        session = _find_session(_load_unlocked(), session_id)
        if session is None:
            raise KeyError(session_id)
        return _session_detail(session)


def history_for_agent(session_id):
    session = get_session(session_id)
    return [
        {"role": message["role"], "content": message["content"]}
        for message in session["messages"]
        if message["role"] in ("user", "assistant")
    ]


def append_exchange(session_id, question, answer, tool_trace=None, usage=None, citations=None):
    question = (question or "").strip()
    if not question:
        raise ValueError("Question is required")

    assistant_content = answer or ""
    trace = deepcopy(tool_trace or [])

    with _LOCK:
        data = _load_unlocked()
        session = _find_session(data, session_id)
        if session is None:
            raise KeyError(session_id)

        if not session.get("messages"):
            session["title"] = _title_from_question(question)

        user_time = _now()
        assistant_time = _now()
        session.setdefault("messages", [])
        session["messages"].append(
            {"role": "user", "content": question, "created_at": user_time}
        )
        session["messages"].append(
            {
                "role": "assistant",
                "content": assistant_content,
                "created_at": assistant_time,
                "tool_trace": trace,
                "usage": deepcopy(usage) if usage else None,
                "citations": deepcopy(citations) if citations else None,
            }
        )
        session["updated_at"] = assistant_time
        _save_unlocked(data)
        return _session_detail(session)


def _empty_usage_bucket():
    bucket = {key: 0 for key in llm_usage.usage_keys()}
    bucket["answers"] = 0
    bucket["model_calls"] = 0
    return bucket


def _add_usage(bucket, usage):
    if not usage:
        return
    bucket["answers"] += 1
    bucket["model_calls"] += len(usage.get("calls") or [])
    for key in llm_usage.usage_keys():
        bucket[key] += int(usage.get(key) or 0)


def usage_summary():
    with _LOCK:
        data = _load_unlocked()
        total = _empty_usage_bucket()
        by_day = {}
        session_count = len(data.get("sessions") or [])
        for session in data.get("sessions") or []:
            for message in session.get("messages") or []:
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage")
                if not usage:
                    continue
                day = (message.get("created_at") or session.get("updated_at") or "")[:10] or "unknown"
                by_day.setdefault(day, _empty_usage_bucket())
                _add_usage(total, usage)
                _add_usage(by_day[day], usage)

    return {
        "session_count": session_count,
        "total": total,
        "by_day": [
            {"date": day, **bucket}
            for day, bucket in sorted(by_day.items(), reverse=True)
        ],
        "unit_note": "nano-AIU is the raw copilot-api usage field; no dollar mapping is applied.",
    }

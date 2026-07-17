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
            "views": deepcopy(message.get("views") or []),
            "feedback": deepcopy(message.get("feedback")) if message.get("feedback") else None,
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


def append_exchange(session_id, question, answer, tool_trace=None, usage=None, citations=None, views=None):
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
                "views": deepcopy(views or []),
            }
        )
        session["updated_at"] = assistant_time
        _save_unlocked(data)
        return _session_detail(session)


def set_feedback(session_id, message_index, vote, comment=""):
    """Attach 👍/👎 feedback to one assistant message. `vote` is 'up', 'down', or '' to clear.

    Feedback lives ON the assistant message (so it reloads with the session and the UI can show the
    prior vote). `list_feedback` derives a flat log from these for later prompt/tool optimization —
    one source of truth, no separate file to drift. Down-votes may carry a free-text `comment`."""
    vote = (vote or "").strip().lower()
    if vote not in ("up", "down", ""):
        raise ValueError("vote must be 'up', 'down', or '' to clear")
    with _LOCK:
        data = _load_unlocked()
        session = _find_session(data, session_id)
        if session is None:
            raise KeyError(session_id)
        messages = session.get("messages") or []
        if not isinstance(message_index, int) or not (0 <= message_index < len(messages)):
            raise IndexError(f"message_index out of range: {message_index}")
        message = messages[message_index]
        if message.get("role") != "assistant":
            raise ValueError("feedback can only attach to an assistant message")
        if not vote:
            message.pop("feedback", None)
            result = None
        else:
            result = {"vote": vote, "comment": (comment or "").strip(), "created_at": _now()}
            message["feedback"] = result
        _save_unlocked(data)
        return result


def list_feedback():
    """Flat, analysis-friendly view of every feedback entry: the vote/comment plus the question and
    answer it refers to. This is the corpus future optimization reads (down-vote comments especially)."""
    with _LOCK:
        data = _load_unlocked()
    out = []
    for session in data.get("sessions") or []:
        messages = session.get("messages") or []
        for index, message in enumerate(messages):
            feedback = message.get("feedback")
            if not feedback:
                continue
            question = ""
            for previous in range(index - 1, -1, -1):
                if messages[previous].get("role") == "user":
                    question = messages[previous].get("content", "")
                    break
            out.append({
                "session_id": session.get("id"),
                "message_index": index,
                "vote": feedback.get("vote"),
                "comment": feedback.get("comment", ""),
                "created_at": feedback.get("created_at"),
                "question": question,
                "answer": message.get("content", ""),
            })
    return out


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

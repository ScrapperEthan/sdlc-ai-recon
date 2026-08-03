"""One short, model-written title per saved session.

The sidebar used to label a session with the first 48 characters of its opening question. For this
app's questions that is the same prefix over and over ("3HK 的 SMS 用例在 UAT 上从昨晚 03:15 开始..."),
which is fine while the session is on screen and useless a week later when the point is to FIND it
again. So the first exchange of a session also asks the model for a short label.

Two rules this module never breaks:

* **It never raises.** A title is cosmetic; the answer the user actually asked for is not. Every
  failure path -- model down, credential expired, rate limited, mock mode, junk output -- degrades
  to `fallback_title`, the same truncation the store used before this existed.
* **It never lets model output through unchecked.** The reply is one line, whitespace-collapsed,
  stripped of the wrapper punctuation and "标题：" prefixes models like to add, and length-capped.
  Anything left empty or implausible falls back rather than putting a paragraph in the sidebar.

Cost: one short model call, on the FIRST exchange of a session only (see server.py -- it passes a
title only when the turn's history is empty). Set SDLC_SESSION_TITLE_LLM=0 to turn it off and keep
the truncation.
"""
from . import config, llm

# Deliberately terse. The model is being asked for a filing label, not a summary paragraph -- the
# failure mode worth spending prompt on is the generic title ("关于系统的问题"), which is exactly as
# useless for retrieval as the truncation it replaces.
_INSTRUCTION = (
    "给下面这轮对话起一个用于会话列表检索的极简标题。\n"
    "要求：\n"
    "1. 只输出标题本身：不要引号、不要句号、不要「标题：」这类前缀、不要解释。\n"
    "2. 最多 20 个汉字（英文最多 40 个字符），一行。\n"
    "3. 用提问所使用的语言。\n"
    "4. 必须点出问的是哪个具体对象（用例 / 仓库 / 渠道 / 供应商 / 告警 / 时间），"
    "不要写成「系统相关问题」「关于消息投递」这种检索不到东西的空话。\n"
)

# How much of the question/answer the title call is allowed to see. A title is not worth a large
# request, and the opening lines carry the subject in practice.
_QUESTION_CHARS = 1200
_ANSWER_CHARS = 600

# Wrapper punctuation models add around a title even when told not to.
_STRIP_CHARS = " \t\r\n\"'`*#·。.、，,：:；;「」『』《》【】()（）[]"
_PREFIXES = ("标题", "title", "session title", "会话标题", "主题")

MAX_TITLE_CHARS = 48


def fallback_title(question, limit=MAX_TITLE_CHARS):
    """The deterministic title: the question, whitespace-collapsed and truncated.

    Used when the model is off/unavailable/unusable. This is the behaviour the store had before AI
    titles existed, so turning the feature off is a real fallback, not a degraded one.
    """
    compact = " ".join((question or "").split())
    if not compact:
        return "New session"
    return compact if len(compact) <= limit else _elide(compact, limit)


def _elide(text, limit):
    """`text` cut to at most `limit` characters INCLUDING the ellipsis."""
    return text[: max(1, limit - 3)].rstrip() + "..."


def _clean(raw):
    """Model output -> a one-line title, or "" if nothing usable came back."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Some models answer with a short preamble then the title; take the first non-empty line.
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    text = " ".join(text.split())
    # "标题：xxx" / "Title: xxx"
    for prefix in _PREFIXES:
        lowered = text.lower()
        if lowered.startswith(prefix):
            remainder = text[len(prefix):].lstrip(" :：-—")
            if remainder:
                text = remainder
                break
    text = text.strip(_STRIP_CHARS)
    if not text:
        return ""
    if len(text) > MAX_TITLE_CHARS:
        text = _elide(text, MAX_TITLE_CHARS)
    return text


def summarize(question, answer=""):
    """A short title for this exchange. Never raises; always returns a non-empty string."""
    question = (question or "").strip()
    if not question:
        return "New session"
    if config.LLM_MOCK or not config.SESSION_TITLE_LLM:
        return fallback_title(question)

    prompt = _INSTRUCTION + "\n提问：\n" + question[:_QUESTION_CHARS]
    if answer:
        prompt += "\n\n回答（节选，仅供判断主题）：\n" + answer[:_ANSWER_CHARS]

    try:
        # No tools: this is a one-shot text call, and handing it the retrieval tool schema would
        # invite the model to start investigating instead of naming what already happened.
        message = llm.chat([{"role": "user", "content": prompt}], temperature=0)
        title = _clean(message.get("content") if isinstance(message, dict) else "")
    except Exception:  # noqa: BLE001 -- a title is never worth failing or delaying a turn for
        return fallback_title(question)
    return title or fallback_title(question)

"""What we are allowed to call on the colleagues' MCP servers, and what it is called over there.

The seam: this side owns the ABSTRACT operations (`log.read`, `aws.alarm_history`, …) and the code
refers only to those. `config/mcp_tools.json` — intranet-owned — says which real tool each one maps
to and what that tool's parameters are actually named. So a rename on their side is a config edit
on the box, not an engine change waiting on external development. RUNBOOK-49/50/51 were three
separate production fixes that all turned out to be edits to one hard-coded vocabulary; this is
that lesson applied before the fact.

Three properties worth being explicit about:

* **Allow-list, not discovery.** Only operations declared in the config may be called. `tools/list`
  is for finding and cross-checking names — a tool appearing there gains no call rights, and MCP
  annotations are hints from a remote server, never an authorization decision.
* **Hard deny beats configuration.** Anything matching `never_expose` is refused even if someone
  declares it, because the damage from calling a resend/submit tool is real and irreversible. The
  assistant is read-only on production; that is not a setting — so the deny list has a copy built
  into this module and config can only ever ADD to it. `SDLC_MCP_TOOLS` points the loader at a
  different file (the box runs a gitignored local one), and a file that simply omits `never_expose`
  must not thereby unlock every action tool.
* **Unwired fails closed and says so.** A `"?"` placeholder means "not filled in yet". Calling such
  an operation raises with the exact list of missing fields, so the assistant reports "this is not
  wired up yet" instead of guessing a parameter name and silently querying the wrong thing.
"""
import fnmatch
import json
import os

from retriever import config as retriever_config

from . import config as webapp_config

UNSET = "?"

# The deny baseline, kept in code so that swapping the config file cannot weaken it. Mirrors
# `never_expose` in config/mcp_tools.json; that file may add entries but never remove these.
# Patterns are matched on the action verb rather than a keyword, deliberately: `check_*_resend_need`
# is a read-only judgement tool and a broad `*resend*` would wrongly bury it (see d6b3a45).
DENY_TOOLS = ("open_portal_login",)
DENY_PATTERNS = (
    "do_*", "execute_*", "perform_*", "trigger_*",
    "*_resend", "resend_*", "*_submit", "submit_*", "*_send", "send_*",
    "delete_*", "remove_*", "update_*", "create_*", "login*", "*_login",
)


class NotWired(RuntimeError):
    """An operation exists but its config still has placeholders."""


class NotAllowed(RuntimeError):
    """The operation is unknown, disabled, or hard-denied."""


def _config_path():
    return os.environ.get("SDLC_MCP_TOOLS") or os.path.join(
        retriever_config.ROOT, "config", "mcp_tools.json")


def load():
    """Unreadable config degrades to nothing callable — but says why.

    `SDLC_MCP_TOOLS` repoints this at another file, so a typo in that env var used to look exactly
    like "no MCP operations exist". Carrying the reason means the status endpoint can name the path
    it failed on instead of costing someone a round trip to find out."""
    path = _config_path()
    try:
        with open(path, encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"servers": {}, "operations": {}, "never_expose": {},
                "_load_error": f"{path}: {exc}"}
    if not isinstance(payload, dict):
        return {"servers": {}, "operations": {},
                "_load_error": f"{path}: expected a JSON object at the top level"}
    return payload


def _clean(mapping):
    """Config dicts carry `_note`/`_README` documentation keys; they are never data."""
    if not isinstance(mapping, dict):
        return {}
    return {k: v for k, v in mapping.items() if not str(k).startswith("_")}


def servers(cfg=None):
    return _clean((cfg or load()).get("servers"))


def server_url(name, cfg=None):
    """Addresses live in env vars, never in git — the config only names which var to read."""
    spec = servers(cfg).get(name) or {}
    return os.environ.get(spec.get("url_env") or "", "")


def operations(cfg=None):
    return _clean((cfg or load()).get("operations"))


def _denied(tool, cfg):
    deny = (cfg.get("never_expose") or {})
    name = (tool or "").lower()
    tools = {str(t).lower() for t in (deny.get("tools") or [])} | set(DENY_TOOLS)
    if name in tools:
        return True
    patterns = list(deny.get("patterns") or []) + list(DENY_PATTERNS)
    return any(fnmatch.fnmatch(name, str(p).lower()) for p in patterns)


def _missing(spec):
    missing = []
    if spec.get("tool", UNSET) == UNSET:
        missing.append("tool")
    for ours, theirs in _clean(spec.get("args")).items():
        if theirs == UNSET:
            missing.append(f"args.{ours}")
    return missing


def readiness(cfg=None):
    """Per-operation wiring status — what the /api/mcp/status endpoint and the box report read.

    Deliberately reports `blocked` separately from `unwired`: "nobody has filled this in" and "this
    must never be called" are different situations and should never be confused for each other."""
    cfg = cfg or load()
    server_specs = servers(cfg)
    out = {}
    for name, spec in operations(cfg).items():
        if not isinstance(spec, dict):
            continue
        server = spec.get("server") or ""
        tool = spec.get("tool") or ""
        missing = _missing(spec)
        if _denied(tool, cfg):
            state = "blocked"
        elif not server_specs.get(server, {}).get("enabled"):
            state = "disabled"
        elif spec.get("tool", UNSET) == UNSET:
            state = "unwired"          # no tool name: nothing about it is callable
        elif missing:
            state = "partial"          # callable, but not with the arguments still marked "?"
        else:
            state = "ready"
        out[name] = {"state": state, "server": server, "tool": tool, "missing": missing,
                     "endpoint_configured": bool(server_url(server, cfg))}
    return out


def summary(cfg=None):
    cfg = cfg or load()
    states = readiness(cfg)
    counts = {}
    for entry in states.values():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return {
        "config_error": cfg.get("_load_error", ""),
        "operations": len(states),
        "by_state": counts,
        "ready": sorted(n for n, e in states.items() if e["state"] == "ready"),
        "servers": {name: {"enabled": bool(spec.get("enabled")),
                           "transport": spec.get("transport") or "",
                           "endpoint_configured": bool(server_url(name, cfg))}
                    for name, spec in servers(cfg).items()},
        "note": ("`unwired` means config/mcp_tools.json still has \"?\" placeholders — the intranet "
                 "side fills those from a live tools/list. Nothing is guessed."),
    }


# ---- the human-readable catalog -------------------------------------------------------------
# What each abstract operation is FOR, in our words. This describes why THIS side calls it, which is
# ours to state; it is not a description of their tool, which is theirs. Their own `description`
# comes off a live `tools/list` and is carried separately and labelled as remote-supplied, because a
# remote server's prose is data we render, never documentation we vouch for and never text that
# reaches a model prompt.
#
# `config/mcp_tools.json` may override any of these per operation via `purpose` — the same seam the
# names, shapes and formats already go through, so the box can write a better sentence without a push.
_PURPOSE = {
    "log.list_apps": "列出某个 source 上有哪些 app（日志目录）。用来把 repo 名对到真实的 app 名 —— "
                     "两者 0% 相同，约 36% 能按规则推出来，其余必须在这里查。",
    "log.browse": "浏览一个 app 的日志目录，看有哪些文件、多大、多新。",
    "log.search_files": "按关键字/文件名/日期找日志文件。log.read 必须先拿到 file_name，"
                        "所以这一步是读日志的前置，不是可选项。",
    "log.read": "读一个日志文件的一段。整条调查链里唯一真正取到生产日志正文的一步。",
    "log.investigate": "他们自己的综合排障接口：给症状和时间，由他们那边决定读什么。",
    "aws.parse_alert": "把一段告警原文交给他们解析。注意：解析结果里的名字我们只做本地严格提取，"
                       "不直接当 alarm name 用（它会把整段话当成名字）。",
    "aws.get_alarm": "读一个告警的定义 —— 指标名、命名空间、维度、阈值、比较方式。"
                     "指标身份只从这里读，绝不从告警名里猜。",
    "aws.alarm_history": "一个告警的状态变迁历史：什么时候进的 ALARM，什么时候恢复。",
    "aws.metric_window": "取告警时间窗内的指标数据点。数据点在进程内算完即弃，"
                         "出去的只有方向/波动/与阈值的关系这类分类结果。",
    "aws.recent_changes": "CloudTrail 最近的变更事件 —— 证据等级最高的一类（谁在事故前动了什么）。",
    "aws.log_groups_for_resource": "一个资源对应哪些 CloudWatch 日志组。query_logs 的前置。",
    "aws.query_logs": "在 CloudWatch 日志组里跑一条查询。返回的是生产日志正文。",
    "aws.resource_tags": "资源上的 tag —— owner 之类的归属信息从这里来。",
    "portal.sms_by_tracking_id": "按 tracking id 查一条短信的投递记录。最大的那个告警家族"
                                 "（General SHP API Error）没有 alarm name 也定位不到 app，"
                                 "这条是唯一的入口。只读。",
    "portal.email_by_tracking_id": "按 tracking id 查一条邮件的投递记录。只读。",
}

# Whether a response is EXPECTED to carry customer-linkable payload. A DEFAULT, not a determination:
# config may override per operation via `data_class`, and — this is the part that matters — nothing
# about redaction depends on it. Every console response is redacted and exit-scanned regardless of
# what this says. It only decides how loudly the panel warns, so a wrong guess here costs a warning
# and never an exposure. Four rounds of intranet review all found the same class of defect: this side
# asserting something about their environment. This is an assertion, so it is built to be harmless.
PAYLOAD_OPERATIONS = frozenset({
    "log.read", "log.investigate", "aws.query_logs",
    "portal.sms_by_tracking_id", "portal.email_by_tracking_id",
})

_SERVER_PURPOSE = {
    "logdream": "同事的应用日志服务：按 app / source 浏览、搜索、读取生产日志文件。",
    "cloudwatch": "AWS 侧：告警定义与历史、指标窗口、CloudTrail 变更、日志组查询、资源 tag。",
    "portal": "投递记录门户：按 tracking id 查单条短信/邮件的投递结果。我们只接只读查询 —— "
              "登录和任何重发类工具永不接入。",
}


def _prose(value):
    """Config `_note` fields are a string or a list of lines; render either as one string."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value or "")


def catalog(cfg=None):
    """Everything the browser needs to SHOW the MCP surface — and nothing it needs to reach it.

    Deliberately excludes endpoints. The addresses live in env vars precisely so they stay out of
    git, and a panel that helpfully printed them would put them in a screenshot instead. What an
    operator actually needs is whether an address is configured, which is a boolean.
    """
    cfg = cfg or load()
    states = readiness(cfg)
    server_specs = servers(cfg)

    ops = {}
    for name, spec in operations(cfg).items():
        if not isinstance(spec, dict):
            continue
        state = states.get(name, {})
        arg_map = _clean(spec.get("args"))
        declared_class = spec.get("data_class")
        ops[name] = {
            "operation": name,
            "server": spec.get("server") or "",
            # Their tool name. Shown because "which of their tools is this" is the first question an
            # operator asks, and because a stale name here is a real failure mode probe() exists for.
            "tool": "" if spec.get("tool", UNSET) == UNSET else (spec.get("tool") or ""),
            "state": state.get("state") or "unwired",
            "missing": list(state.get("missing") or []),
            "purpose": str(spec.get("purpose") or _PURPOSE.get(name) or ""),
            "data_class": (declared_class if declared_class in ("payload", "metadata")
                           else ("payload" if name in PAYLOAD_OPERATIONS else "metadata")),
            # Per argument: our name, their name, and whether it can be passed yet. The console
            # builds its form from this, so an unwired argument is a disabled field with a reason
            # rather than a box that silently sends the wrong parameter name.
            "args": [{"name": ours, "their_name": "" if theirs == UNSET else str(theirs),
                      "wired": theirs != UNSET}
                     for ours, theirs in sorted(arg_map.items())],
            # Names only — a const is a pinned value on their side, and printing values invites
            # someone to edit one in the panel, which is exactly what pinning them prevents.
            "const_keys": sorted(_clean(spec.get("const"))),
            "note": _prose(spec.get("_note")),
            "callable": state.get("state") == "ready" or state.get("state") == "partial",
        }

    out = {
        "servers": {},
        "operations": ops,
        "calling_enabled": bool(webapp_config.MCP_ENABLED),
        "config_error": cfg.get("_load_error", ""),
        "config_path": _config_path(),
    }
    for name, spec in server_specs.items():
        members = sorted(op for op, entry in ops.items() if entry["server"] == name)
        out["servers"][name] = {
            "name": name,
            "enabled": bool(spec.get("enabled")),
            "transport": "" if spec.get("transport", UNSET) == UNSET else (spec.get("transport") or ""),
            "endpoint_configured": bool(server_url(name, cfg)),
            "url_env": spec.get("url_env") or "",
            "purpose": str(spec.get("purpose") or _SERVER_PURPOSE.get(name) or ""),
            "note": _prose(spec.get("_note")),
            "operations": members,
            "ready": sum(1 for op in members if ops[op]["state"] == "ready"),
        }
    # Operations naming a server the config never declared would otherwise vanish from the panel
    # while still being callable-looking in the config. Surface them rather than hide them.
    for name, entry in ops.items():
        if entry["server"] and entry["server"] not in out["servers"]:
            out["servers"][entry["server"]] = {
                "name": entry["server"], "enabled": False, "transport": "",
                "endpoint_configured": False, "url_env": "", "purpose": "",
                "note": "declared by an operation but missing from `servers` in the config",
                "operations": [name], "ready": 0}
    return out


def build_call(operation, args=None, cfg=None):
    """Translate one abstract call into (server, tool, their_params).

    Raises rather than improvising: an incident answer built on a wrongly-named parameter is worse
    than an answer that says the integration is not ready."""
    cfg = cfg or load()
    spec = operations(cfg).get(operation)
    if not isinstance(spec, dict):
        raise NotAllowed(
            f"unknown MCP operation: {operation!r}. Only operations declared in "
            f"config/mcp_tools.json may be called; discovery via tools/list grants no access.")

    tool = spec.get("tool") or ""
    if _denied(tool, cfg):
        raise NotAllowed(
            f"operation {operation!r} maps to {tool!r}, which is on the never_expose deny list. "
            f"The assistant is read-only on production; action-taking tools are never callable.")

    server = spec.get("server") or ""
    if not servers(cfg).get(server, {}).get("enabled"):
        raise NotAllowed(f"MCP server {server!r} is not enabled in config/mcp_tools.json")

    if spec.get("tool", UNSET) == UNSET:
        raise NotWired(
            f"operation {operation!r} has no tool name yet — fill `tool` in config/mcp_tools.json "
            f"from a live tools/list on the box.")

    # Only the arguments actually being passed need a mapping. An unfilled placeholder for an
    # argument nobody is using is irrelevant, so partial wiring is usable for the parts that ARE
    # wired — the intranet side can fill this in one field at a time and each one works as it lands.
    params = dict(_clean(spec.get("const")))
    arg_map = _clean(spec.get("args"))
    for ours, value in (args or {}).items():
        if value is None:
            continue     # an unsupplied optional argument is simply not sent
        theirs = arg_map.get(ours)
        if theirs is None:
            raise NotWired(
                f"operation {operation!r} has no mapping for argument {ours!r}; add it to "
                f"config/mcp_tools.json args.")
        if theirs == UNSET:
            raise NotWired(
                f"operation {operation!r} cannot pass {ours!r} yet — its parameter name is still "
                f"\"?\" in config/mcp_tools.json. Fill it from a live tools/list on the box.")
        params[theirs] = value
    return server, tool, params

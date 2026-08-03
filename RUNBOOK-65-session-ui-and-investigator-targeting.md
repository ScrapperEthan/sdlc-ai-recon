# RUNBOOK-65 — session sidebar, the raw-log viewer, and telling the investigator where to look

Covers two pushes (`dac235f` + this one). Nothing here needs an MCP call except part D, which is
the only part that reads production.

Run on the box with the webapp up. Report per check: PASS / FAIL + what you saw.

---

## A. The investigator panel survives a reload

Before this, the 事故调查员 panel was streamed only — refreshing the page erased it.

1. Ask an incident question that actually runs the investigator (any alert with a full
   `YYYY-MM-DD HH:MM` **and** a timezone).
2. While it runs, note the step count in the panel header (`事故调查员 正在调查 · N 步`).
3. When it finishes, **reload the page**.

**PASS** — the panel is still there, headed `事故调查员 已完成 · N 步 · M 条证据`, with the same
steps, the same `logdream · log.read` / `cloudwatch · aws.get_alarm` badges and the same latencies.

**Also check** (this is the property the design turns on): the model must NOT be re-fed the steps.
Ask a follow-up in the same session, then look at `webapp_data/chat_sessions.json` — the steps live
on the assistant message under `subagent_steps`, and `history_for_agent` sends only `role`/`content`.
Raw log text must still appear **only** in `webapp_data/incident_raw.json`, never in the session file:

```bash
python -c "import json,sys; d=json.load(open('webapp_data/chat_sessions.json',encoding='utf-8')); print('raw_ref count:', json.dumps(d).count('raw_ref'))"
```

Refs are expected; log LINES are not. FAIL if you can read log text in the session file.

## B. Long session list no longer collides with Retrieval Tools

1. Create enough sessions that the list is taller than the sidebar (~8+).
2. Expand **Operating Mode** and **Retrieval Tools**.

**PASS** — the session list scrolls inside its own box; the two sections below stay put and are
fully readable. Nothing draws on top of anything.

## C. AI session titles + search

1. Start a new session and ask something specific ("3HK 的 SMS 用例昨晚 03:15 HKT 开始投递失败").
2. Look at the sidebar after the answer lands.

**PASS** — the title is a short label about THAT question, not the first 48 characters of it.
Second and later turns must NOT rename the session.

3. Set `SDLC_SESSION_TITLE_LLM=0`, restart, ask again → title falls back to the truncated question.
   Same expected if the model endpoint is down: **the answer must still be saved**.
4. Type a word from an ANSWER (not a title) into the sidebar search box.

**PASS** — only matching sessions are listed, each showing where it matched (标题 / 我的提问 / 回答)
and the surrounding text with the term highlighted. `×` or Escape restores the full list.

## D. 查看原文 is readable — and part D is the only production read

Needs `SDLC_INCIDENT_RAW_LOGS=1` (UAT only) and a real investigation with evidence.

1. Click **查看原文** on an evidence step.

**PASS** — a full-height drawer opens (not a small inline box), red-banded, headed
`⚠ 未脱敏生产日志原文 · 共 N 行 · 存于 <stamp>`, with numbered lines at readable size.
Escape closes it. The 自动换行 checkbox toggles wrapping. Opening a `repo/path:line` citation while
it is open must close it (one drawer at a time).

2. Confirm it is still owner-scoped: open the app in a different browser profile and try the same
   session — it must not be reachable.

## E. Telling the investigator WHICH service to look at  ← the new behaviour

This is the part most worth adversarial testing; the previous four rounds all found defects here.

1. Take an alert that names **no repo** — e.g. `MDC Alert - General SHP API Error`, with a full
   timestamp and timezone. Ask for a root-cause investigation without naming a service.

   **Expected**: it refuses with "nothing to query… or pass `repos` if you already know which
   service it is". Zero MCP calls.

2. Now tell it the service in plain language ("是 &lt;一个真实的、你们用 list_repos 查出来的精确 repo id&gt; 这个服务").

   **Expected**: the model passes `repos`, the plan becomes runnable, and the investigation runs
   against that app. In the packet, `plan.targets[].source` must read **"supplied by the caller"**,
   and `plan.targets_note` must be present. The ANSWER must say the target was one you supplied,
   not one the alert identified.

3. Name a service that does not exist (`mc-hk-hase-not-a-real-repo`).

   **Expected**: refused — `"…not in the repo universe… this is a wrong name, not an empty log"` —
   and **no query is sent for it**. FAIL if it queries anything, and FAIL HARD if an empty result
   is reported as "the logs are clean".

4. Ask something that should take more than one sweep ("先看 SocketTimeout，没有的话再看
   ConnectException，还要看下游那个服务").

   **Expected**: `incident_investigate` is called **more than once in the same turn**, each call
   changing something (`keywords`, `sources`, `repos`, `max_queries`). The frontend shows a separate
   事故调查员 panel per call. The answer says how many sweeps ran and what each covered.

5. Ask about an alert with a bare `03:15` and no date/zone.

   **Expected**: exactly ONE refusal round trip — it asks which day / which zone and does **not**
   keep re-calling the tool. Re-calling cannot fix a blocking window refusal.

**Report back**: for step 2, paste `plan.targets` and `queries_executed`; for step 4, how many
sweeps and what each changed.

---

## What to send back

- PASS/FAIL per check.
- For any FAIL: the packet's `plan`, `queries_attempted`, `queries_executed`, `not_investigated`.
- Whether the model ever passed a `repos` id that was not a real repo (step 3 is the guard, but
  frequency tells us whether the schema wording is strong enough).

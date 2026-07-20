# RUNBOOK 44 (INTERNAL Codex / you) — verify per-browser session/feedback isolation

> **Pull `master` (`00b7aa4`+), then confirm RUNBOOK-43 actually isolates users on the real box.**
> `GET /api/sessions`, `GET /api/sessions/<id>`, and `GET /api/feedback` used to be global —
> anyone could read anyone else's chat history and feedback. The fix (an auto-issued `sdlc_uid`
> cookie, no login/SSO) has only been unit-tested locally against a temp JSON store; it has never
> run against the real box's `webapp_data/chat_sessions.json` or through a real browser's cookie
> jar. This is the first real check.

## Step 1 — pull + restart + sanity
```
git pull
```
Restart both services (`start.bat`, or your usual two-terminal launch) so the new code is live.
```
python -m unittest discover -s . -p "test_*.py"
```
Expect **167 OK**.

## Step 2 — scripted check: two users via curl (deterministic, no browser needed)

Two different `sdlc_uid` cookie values stand in for two different testers. Replace `HOST` with
wherever the chat is reachable (e.g. `http://127.0.0.1:8765`).

```
# User A asks a question, no session_id -> server creates + owns a session for "uid-a"
curl -s -X POST HOST/api/chat -H "Cookie: sdlc_uid=uid-a" -H "Content-Type: application/json" \
  -d '{"question":"who consumes X?"}' | tee /tmp/a.json

# User B does the same, as "uid-b"
curl -s -X POST HOST/api/chat -H "Cookie: sdlc_uid=uid-b" -H "Content-Type: application/json" \
  -d '{"question":"who produces Y?"}' | tee /tmp/b.json
```
Note each response's `session.id` (call them `SID_A`, `SID_B`).

```
# A's session list must show ONLY SID_A
curl -s HOST/api/sessions -H "Cookie: sdlc_uid=uid-a"

# B's session list must show ONLY SID_B
curl -s HOST/api/sessions -H "Cookie: sdlc_uid=uid-b"

# B trying to read A's session directly by id -> must be 404, not A's content
curl -s -o /dev/null -w "%{http_code}\n" HOST/api/sessions/SID_A -H "Cookie: sdlc_uid=uid-b"

# A voting on their OWN answer -> should succeed (200)
curl -s -X POST HOST/api/feedback -H "Cookie: sdlc_uid=uid-a" -H "Content-Type: application/json" \
  -d '{"session_id":"SID_A","message_index":1,"vote":"down","comment":"test"}'

# B trying to vote on A's session -> must be 404
curl -s -X POST HOST/api/feedback -H "Cookie: sdlc_uid=uid-b" -H "Content-Type: application/json" \
  -d '{"session_id":"SID_A","message_index":1,"vote":"up"}'

# A's feedback list shows the down-vote; B's feedback list must NOT show it
curl -s HOST/api/feedback -H "Cookie: sdlc_uid=uid-a"
curl -s HOST/api/feedback -H "Cookie: sdlc_uid=uid-b"
```

## Step 3 — cookie auto-issuance actually works (no manual cookie)
```
curl -si HOST/ | grep -i "set-cookie"
```
A fresh request with **no** cookie at all must get back a `Set-Cookie: sdlc_uid=...` header. Re-run
the same curl WITH that cookie attached (`-H "Cookie: sdlc_uid=<value>"`) — this second response
must NOT carry another `Set-Cookie` (already has one, no re-issue).

## Step 4 — real browser check (confirms the frontend actually keeps the cookie)
Open the chat in two different browsers (or one normal + one incognito/private window so cookies
don't share), ask one question in each, and confirm:
- Each browser's session sidebar shows only the session it created, not the other's.
- Refreshing the page keeps seeing the SAME session (cookie persisted), not a fresh empty list.

## Send back
```
Step 1  [ 167 tests OK? ]
Step 2  [ A's list = [SID_A] only? B's list = [SID_B] only? B->SID_A = 404?
          B voting on A's session = 404? A's feedback list has the entry, B's doesn't? ]
Step 3  [ cookie-less request gets Set-Cookie? repeat request with that cookie does NOT re-issue? ]
Step 4  [ two browsers each see only their own session in the sidebar, survives refresh? ]
```

## Notes
- Old sessions created before this deploy have no `owner` field and will not appear via the API for
  ANYONE (by design — an unowned session never matches any caller). They're still on disk in
  `webapp_data/chat_sessions.json` if anyone needs to recover something from them manually.
- `GET /api/usage` (aggregate token/cost counts, no Q&A content) is intentionally NOT scoped by user
  — that's a deliberate scope decision, not a gap to report.
- This is cookie-based identity, not real login: clearing cookies or switching machines loses
  access to prior sessions, and two testers sharing one browser profile would share one identity.
  Known limitation, accepted for this phase — not a bug to flag.

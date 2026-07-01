# Spec: clickable citations → source viewer

**Audience:** external Codex IMPLEMENTS; internal Codex VERIFIES. Read `BACKLOG.md`
"Project context" + "Guardrails" first. Pairs well with `citation-verification.md`.

## Goal

Click a `repo/path:line` citation pill and see the actual cited source, with the
target line highlighted. Turns "trust me" into "look yourself."

## Where

- `webapp/server.py` — new read-only `GET /api/source`.
- `retriever/code.py` — a structured window reader (reuse `read_file` logic).
- `webapp/static/index.html` — click handler on `.cite` pills + a slide-in panel.

## Design decisions (made)

- Endpoint returns a WINDOW around the line (default ±40), not whole files.
- **Security is the point:** the requested path must resolve to a real file INSIDE
  `mirror/`. Reject `..`, absolute paths, symlinks escaping the mirror → `403`.
- Read-only, stdlib only. The pill text is the source of the path+line.

## Backend

### `retriever/code.py` — add

```python
def read_window(relpath, line=None, ctx=40):
    """Return a structured window inside the mirror, or raise ValueError on a
    path that escapes mirror/."""
    mirror_real = os.path.realpath(config.MIRROR)
    full = os.path.realpath(os.path.join(config.MIRROR, *relpath.split("/")))
    if not (full == mirror_real or full.startswith(mirror_real + os.sep)):
        raise ValueError("path escapes mirror")
    if not os.path.isfile(full):
        raise FileNotFoundError(relpath)
    with open(full, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    total = len(all_lines)
    if line:
        start = max(1, line - ctx); end = min(total, line + ctx)
    else:
        start, end = 1, min(total, 2 * ctx + 1)
    return {
        "path": relpath, "total": total, "start": start, "end": end, "line": line,
        "lines": [{"n": i, "text": all_lines[i - 1].rstrip("\n")} for i in range(start, end + 1)],
    }
```

### `webapp/server.py` — add to `do_GET`

```python
elif path == "/api/source":
    from urllib.parse import parse_qs
    qs = parse_qs(urlparse(self.path).query)
    rel = (qs.get("path") or [""])[0]
    line = int((qs.get("line") or ["0"])[0] or 0) or None
    try:
        from retriever import code as rcode
        self._send_json(200, rcode.read_window(rel, line))
    except ValueError:
        self._send_json(403, {"error": "forbidden path"})
    except FileNotFoundError:
        self._send_json(404, {"error": "not found"})
```

## Frontend (`webapp/static/index.html`)

Pill text looks like `mc-hk-hase-.../IngressResource.java:69` (or bare
`IngressResource.java:69`). Parse `path` + `line`, fetch `/api/source`, open a panel.

```js
function parseCite(txt) {
  const m = (txt || '').trim().match(/^(.*?)(?::(\d+))?(?:-\d+)?$/);
  return m ? { path: m[1], line: m[2] ? Number(m[2]) : null } : null;
}

async function openSource(ref) {
  const c = parseCite(ref); if (!c) return;
  const url = '/api/source?path=' + encodeURIComponent(c.path) + (c.line ? '&line=' + c.line : '');
  const panel = document.getElementById('source-panel');
  panel.hidden = false;
  panel.querySelector('.src-title').textContent = ref;
  const body = panel.querySelector('.src-body'); body.textContent = 'Loading…';
  try {
    const d = await (await fetch(url)).json();
    if (d.error) { body.textContent = d.error; return; }
    body.innerHTML = '';
    d.lines.forEach((ln) => {
      const row = document.createElement('div');
      row.className = 'src-line' + (ln.n === d.line ? ' hit' : '');
      const num = document.createElement('span'); num.className = 'src-n'; num.textContent = ln.n;
      const code = document.createElement('span'); code.className = 'src-t'; code.textContent = ln.text;
      row.append(num, code); body.appendChild(row);
    });
    const hit = body.querySelector('.hit'); if (hit) hit.scrollIntoView({block:'center'});
  } catch (e) { body.textContent = 'Failed: ' + e; }
}

// event delegation (works for pills added later, streamed, or from loaded sessions)
document.addEventListener('click', (e) => {
  const pill = e.target.closest('code.cite');
  if (pill) openSource(pill.textContent);
});
```

Add markup once (near the app shell) and CSS:
```html
<aside id="source-panel" hidden>
  <div class="src-head"><span class="src-title"></span>
    <button onclick="document.getElementById('source-panel').hidden=true">✕</button></div>
  <div class="src-body"></div>
</aside>
```
CSS: fixed right-side panel (or modal), monospace, `.src-line` grid `auto 1fr`,
`.src-n` muted, `.hit` highlighted background. Also make `code.cite { cursor: pointer }`.

## Keep working

- No change to `/api/chat`, sessions, streaming, evals. Purely additive read path.

## Done when

1. Clicking a real citation opens the correct file window with the target line
   highlighted and scrolled into view.
2. `GET /api/source?path=../../etc/passwd` (and any path escaping `mirror/`) → `403`.
3. Bare-filename citations resolve if the file path is complete; otherwise show 404
   gracefully (no crash).

## Verification steps (internal Codex)

```bash
curl -s 'http://127.0.0.1:8765/api/source?path=mc-hk-hase-ingress-api/src/main/java/com/hsbc/hase/digital/api/ingress/resource/IngressResource.java&line=69' | head
curl -s -o /dev/null -w "%{http_code}\n" 'http://127.0.0.1:8765/api/source?path=../secret'
```
Expect a JSON window for the first, `403` for the second. Then click pills in the UI.

## Notes

- Pill text may or may not include the repo prefix. The backend tries mirror-relative
  first; if you find bare filenames common, add a unique-basename fallback in
  `read_window` (mirror the resolver in `retriever/citations.py`).

/* Everything the page does. Split out of index.html for the same reason as app.css.
   Loaded as a classic script at the position the inline <script> occupied — after the markup it
   reads at load time, before the mermaid vendor bundle — so execution order is unchanged. Not a
   module: the inline onclick= handlers in the markup call these functions by name off `window`. */

const log = document.getElementById('log'), q = document.getElementById('q'),
      f = document.getElementById('f'), send = document.getElementById('send'),
      sessionListEl = document.getElementById('session-list'),
      sessionSearchEl = document.getElementById('session-search'),
      sessionSearchClearEl = document.getElementById('session-search-clear'),
      newSessionButton = document.getElementById('new-session'),
      sessionMeta = document.getElementById('session-meta'),
      indexStatus = document.getElementById('index-status'),
      mcpStatus = document.getElementById('mcp-status'),
      usageButton = document.getElementById('usage-button'),
      usagePanel = document.getElementById('usage-panel'),
      empty = document.getElementById('empty');
const history = [];
let currentSessionId = null;
let sessions = [];
// Search is a VIEW over the sidebar, never over `sessions` itself: `sessions` stays the full list so
// currentSessionId resolution (refreshSessions) is unaffected by whatever is typed in the box.
let sessionQuery = '';
let sessionSearchResults = null;   // null = not searching; [] = searched, nothing matched

function hideEmptyState() {
  if (empty) empty.hidden = true;
}

function resetLog(showEmptyState = true) {
  while (log.firstChild) log.removeChild(log.firstChild);
  empty.hidden = !showEmptyState;
  log.appendChild(empty);
}

function setSessionMeta(text) {
  sessionMeta.textContent = text;
}

function setBusy(isBusy) {
  send.disabled = isBusy;
  sessionListEl.classList.toggle('is-busy', isBusy);
  newSessionButton.disabled = isBusy;
  f.setAttribute('aria-busy', String(isBusy));
}

// Highlight every occurrence of the search term inside a snippet, without ever putting server text
// through innerHTML — each fragment is appended as a text node, only the <mark> is created by us.
function appendHighlighted(target, text, needle) {
  const haystack = text || '';
  const term = (needle || '').trim();
  if (!term) { target.appendChild(document.createTextNode(haystack)); return; }
  const lower = haystack.toLowerCase(), lowerTerm = term.toLowerCase();
  let from = 0, at;
  while ((at = lower.indexOf(lowerTerm, from)) >= 0) {
    if (at > from) target.appendChild(document.createTextNode(haystack.slice(from, at)));
    const hit = document.createElement('mark');
    hit.textContent = haystack.slice(at, at + term.length);
    target.appendChild(hit);
    from = at + term.length;
  }
  if (from < haystack.length) target.appendChild(document.createTextNode(haystack.slice(from)));
}

const MATCH_LABELS = {title: '标题', user: '我的提问', assistant: '回答'};

function renderSessionList(selectedId = '') {
  sessionListEl.innerHTML = '';
  const searching = sessionSearchResults !== null;
  const rows = searching ? sessionSearchResults : sessions;

  if (!rows.length) {
    const emptyNote = document.createElement('p');
    emptyNote.className = 'session-list-empty';
    emptyNote.textContent = searching
      ? `No session matches "${sessionQuery}"`
      : 'No saved sessions yet';
    sessionListEl.appendChild(emptyNote);
    return;
  }

  rows.forEach((session) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'session-item' + (session.id === selectedId ? ' active' : '');
    item.dataset.sessionId = session.id;
    item.setAttribute('role', 'listitem');
    if (session.id === selectedId) item.setAttribute('aria-current', 'true');

    const title = document.createElement('span');
    title.className = 'session-item-title';
    title.textContent = session.title || 'New session';
    title.title = session.title || 'New session';   // the row ellipsises; the tooltip does not

    const count = Number(session.message_count || 0);
    const meta = document.createElement('span');
    meta.className = 'session-item-meta';
    const where = ((session.match && session.match.in) || [])
      .map((k) => MATCH_LABELS[k] || k).join(' · ');
    meta.textContent = (count ? `${count} message${count === 1 ? '' : 's'}` : 'empty')
      + (where ? ` · 命中：${where}` : '');

    item.appendChild(title);
    item.appendChild(meta);
    if (session.match && session.match.snippet) {
      const snippet = document.createElement('span');
      snippet.className = 'session-item-snippet';
      appendHighlighted(snippet, session.match.snippet, sessionQuery);
      item.appendChild(snippet);
    }
    sessionListEl.appendChild(item);
  });
}

// Debounced so typing doesn't fire one scan of the store per keystroke, and revision-guarded so a
// slower earlier query can never overwrite the results of a newer one.
let sessionSearchTimer = null;
let sessionSearchRevision = 0;

async function runSessionSearch(query) {
  const revision = ++sessionSearchRevision;
  sessionQuery = query.trim();
  sessionSearchClearEl.hidden = !sessionQuery;
  if (!sessionQuery) {
    sessionSearchResults = null;
    renderSessionList(currentSessionId);
    return;
  }
  try {
    const data = await fetchJson('/api/sessions?q=' + encodeURIComponent(sessionQuery));
    if (revision !== sessionSearchRevision) return;
    sessionSearchResults = data.sessions || [];
  } catch (error) {
    if (revision !== sessionSearchRevision) return;
    sessionSearchResults = [];
  }
  renderSessionList(currentSessionId);
}

function clearSessionSearch() {
  clearTimeout(sessionSearchTimer);
  sessionSearchEl.value = '';
  sessionSearchRevision++;   // abandon anything in flight
  sessionQuery = '';
  sessionSearchResults = null;
  sessionSearchClearEl.hidden = true;
  renderSessionList(currentSessionId);
}

sessionSearchEl.addEventListener('input', () => {
  clearTimeout(sessionSearchTimer);
  const value = sessionSearchEl.value;
  sessionSearchTimer = setTimeout(() => runSessionSearch(value), 180);
});
sessionSearchEl.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') clearSessionSearch();
});
sessionSearchClearEl.addEventListener('click', clearSessionSearch);

function syncHistory(messages) {
  history.length = 0;
  (messages || []).forEach((message) => {
    if (message.role === 'user' || message.role === 'assistant') {
      history.push({role: message.role, content: message.content || ''});
    }
  });
}

function renderSessionMessages(messages) {
  if (!messages || !messages.length) {
    resetLog(true);
    syncHistory([]);
    return;
  }

  resetLog(false);
  messages.forEach((message, index) => {
    const who = message.role === 'user' ? 'you' : 'assistant';
    const container = add(message.role, message.content || '', who);
    if (message.role === 'assistant' && message.tool_trace && message.tool_trace.length) {
      addToolTrace(container, message.tool_trace);
    }
    // The investigator panel is part of the answer, not a progress spinner: it is the only place
    // that says which production system was contacted and what came back. It used to exist only in
    // the live stream, so a page reload silently deleted it. Replayed here as completed.
    if (message.role === 'assistant' && message.subagent_steps && message.subagent_steps.length) {
      showSubagent(container, message.subagent_steps, true);
    }
    if (message.role === 'assistant' && message.views && message.views.length) {
      message.views.forEach((v) => renderInlineView(container, v));
    }
    if (message.role === 'assistant' && message.citations) {
      markCitations(container.querySelector('.bubble'), message.citations);
    }
    if (message.role === 'assistant' && message.usage) {
      renderUsage(container, message.usage);
    }
    if (message.role === 'assistant') {
      renderFeedback(container, currentSessionId, index, message.feedback);
    }
  });
  syncHistory(messages);
}

function updateSessionMetaFromSession(session) {
  if (!session) {
    setSessionMeta('Sessions are saved locally in JSON for internal testing.');
    return;
  }
  const count = Number(
    session.message_count != null ? session.message_count : (session.messages || []).length
  );
  const messageLabel = `${count} message${count === 1 ? '' : 's'}`;
  setSessionMeta(`Saved locally in JSON. ${messageLabel} in "${session.title || 'New session'}".`);
}

// Each browser carries a pairing token that selects its OWN LLM endpoint on the server. Sent on
// every request; absent => the server falls back to its default LLM.
let userToken = localStorage.getItem('sdlc_user_token') || '';
function authHeaders() { return userToken ? {'X-SDLC-User-Token': userToken} : {}; }

async function fetchJson(url, options = {}) {
  const headers = {...(options.headers || {}), ...authHeaders()};
  if (options.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }

  const response = await fetch(url, {...options, headers});
  const raw = await response.text();
  const data = raw ? JSON.parse(raw) : {};

  if (!response.ok) {
    // Preserve HTTP semantics from the structured LLM endpoints. Callers use these attributes to
    // refresh stale Token UI after 401/403 and to give a useful wait/retry hint for a 429; the
    // server's message remains the user-safe, sanitized text.
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.code = data.code || '';
    error.retryAfter = data.retry_after;
    error.retryable = !!data.retryable;
    error.reconnectRequired = !!data.reconnect_required;
    throw error;
  }
  return data;
}

async function refreshSessions(preferredId = currentSessionId) {
  const data = await fetchJson('/api/sessions');
  sessions = data.sessions || [];

  const nextId = sessions.some((session) => session.id === preferredId)
    ? preferredId
    : (sessions[0] && sessions[0].id) || '';

  currentSessionId = nextId || null;
  updateSessionMetaFromSession(sessions.find((session) => session.id === currentSessionId));
  // A search view goes stale the moment a new answer lands (new title, new message text), so re-run
  // it rather than leaving the sidebar showing results that no longer describe the store.
  if (sessionQuery) await runSessionSearch(sessionQuery);
  else renderSessionList(currentSessionId);
  return sessions;
}

async function loadSession(sessionId) {
  if (!sessionId) {
    currentSessionId = null;
    resetLog(true);
    syncHistory([]);
    updateSessionMetaFromSession(null);
    renderSessionList('');
    return;
  }

  const session = await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
  currentSessionId = session.id;
  renderSessionMessages(session.messages || []);
  updateSessionMetaFromSession(session);
  await refreshSessions(session.id);
}

async function createSession() {
  const session = await fetchJson('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({title: 'New session'})
  });
  currentSessionId = session.id;
  renderSessionMessages(session.messages || []);
  updateSessionMetaFromSession(session);
  await refreshSessions(session.id);
  return session;
}

async function initializeSessions() {
  setBusy(true);
  // The browser restores an <input type="search"> value across a reload, so the state behind the box
  // has to be READ from it, not assumed empty — otherwise the sidebar can list every session under a
  // search term that is still visibly typed in. Seeding it here makes the two agree either way:
  // refreshSessions re-runs the search whenever sessionQuery is set.
  sessionQuery = sessionSearchEl.value.trim();
  sessionSearchClearEl.hidden = !sessionQuery;
  try {
    const storedSessions = await refreshSessions();
    if (storedSessions.length) {
      await loadSession(currentSessionId);
    } else {
      resetLog(true);
      syncHistory([]);
      updateSessionMetaFromSession(null);
    }
  } catch (error) {
    resetLog(true);
    syncHistory([]);
    setSessionMeta(`Failed to load saved sessions: ${error.message}`);
  } finally {
    setBusy(false);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]
  ));
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/`/g, '&#96;');
}

function sanitizeUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl, window.location.origin);
    if (['http:', 'https:', 'mailto:'].includes(parsed.protocol)) {
      return parsed.href;
    }
  } catch (error) {
    return null;
  }
  return null;
}

function renderInlineMarkdown(text) {
  const placeholders = [];
  const stash = (html) => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push(html);
    return token;
  };

  let rendered = String(text);

  rendered = rendered.replace(/`([^`\n]+)`/g, (_, code) => (
    stash(`<code>${escapeHtml(code)}</code>`)
  ));

  rendered = rendered.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, url) => {
    const safeUrl = sanitizeUrl(url);
    if (!safeUrl) return stash(escapeHtml(label));
    return stash(
      `<a href="${escapeAttribute(safeUrl)}" target="_blank" rel="noreferrer noopener">${escapeHtml(label)}</a>`
    );
  });

  rendered = rendered.replace(/<((?:https?:\/\/|mailto:)[^>\s]+)>/g, (_, url) => {
    const safeUrl = sanitizeUrl(url);
    if (!safeUrl) return stash(escapeHtml(url));
    return stash(
      `<a href="${escapeAttribute(safeUrl)}" target="_blank" rel="noreferrer noopener">${escapeHtml(url)}</a>`
    );
  });

  rendered = escapeHtml(rendered)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])__([^_\n]+)__(?=[^\w]|$)/g, '$1<strong>$2</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/(^|[^\w])_([^_\n]+)_(?=[^\w]|$)/g, '$1<em>$2</em>')
    .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
    .replace(/\n/g, '<br>');

  return rendered.replace(/\u0000(\d+)\u0000/g, (_, index) => placeholders[Number(index)] || '');
}

function isTableSeparator(line) {
  const compact = line.trim();
  return /^\|?[\s:-]+(?:\|[\s:-]+)+\|?$/.test(compact);
}

function splitTableRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function renderTable(lines) {
  const headers = splitTableRow(lines[0]);
  const rows = lines.slice(2).map(splitTableRow);
  const headerHtml = headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join('');
  const bodyHtml = rows.map((cells) => (
    `<tr>${cells.map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join('')}</tr>`
  )).join('');

  return `<table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`;
}

function renderMarkdown(text) {
  const source = String(text || '').replace(/\r\n?/g, '\n');
  const lines = source.split('\n');
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fenceMatch = line.match(/^```([\w-]+)?\s*$/);
    if (fenceMatch) {
      const lang = fenceMatch[1] || '';
      const codeLines = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      const fenceBody = codeLines.join('\n');
      if (lang.toLowerCase() === 'mermaid') {
        // Rendered to a live diagram after the message completes (renderMermaid). The escaped
        // source stays as the text content, so it degrades to readable text if mermaid is absent.
        blocks.push(`<pre class="mermaid">${escapeHtml(fenceBody)}</pre>`);
      } else {
        blocks.push(
          `<pre><code${lang ? ` data-lang="${escapeAttribute(lang)}"` : ''}>${escapeHtml(fenceBody)}</code></pre>`
        );
      }
      continue;
    }

    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      blocks.push('<hr>');
      i += 1;
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(headingMatch[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    if (line.trim().startsWith('>')) {
      const quoteLines = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        quoteLines.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      blocks.push(`<blockquote>${renderMarkdown(quoteLines.join('\n'))}</blockquote>`);
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const tableLines = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        tableLines.push(lines[i]);
        i += 1;
      }
      blocks.push(renderTable(tableLines));
      continue;
    }

    const listMatch = line.match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = /\d+\./.test(listMatch[2]);
      const tag = ordered ? 'ol' : 'ul';
      const items = [];
      while (i < lines.length) {
        const itemMatch = lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/);
        if (!itemMatch || /\d+\./.test(itemMatch[2]) !== ordered) break;
        items.push(`<li>${renderInlineMarkdown(itemMatch[3].trim())}</li>`);
        i += 1;
      }
      blocks.push(`<${tag}>${items.join('')}</${tag}>`);
      continue;
    }

    const paragraphLines = [line];
    i += 1;
    while (i < lines.length && lines[i].trim()) {
      if (
        /^```/.test(lines[i]) ||
        /^(#{1,6})\s+/.test(lines[i]) ||
        /^\s*>\s?/.test(lines[i]) ||
        /^(\s*)([-*+]|\d+\.)\s+/.test(lines[i]) ||
        /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(lines[i]) ||
        (lines[i].includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1]))
      ) {
        break;
      }
      paragraphLines.push(lines[i]);
      i += 1;
    }
    blocks.push(`<p>${renderInlineMarkdown(paragraphLines.join('\n'))}</p>`);
  }

  return blocks.join('');
}

const CITE_RE = /(?:[\w.\-\/]+)?[\w.\-]+\.(?:java|xml|ya?ml|properties|kts?|json|sql)(?::\d+(?:-\d+)?)?$|:\d+(?:-\d+)?$/i;
const EVIDENCE_RE = /^(evidence|verified|what i verified|unverified|partial|references?|证据|依据|验证)/i;

function decorateAnswer(bubble) {
  // 1) make repo/path:line references stand out as evidence pills
  bubble.querySelectorAll('code').forEach((c) => {
    if (CITE_RE.test((c.textContent || '').trim())) c.classList.add('cite');
  });
  // 2) fold everything from the first "Evidence"-style heading into a labeled block
  if (bubble.querySelector('.evidence-section')) return;
  const heads = Array.from(bubble.querySelectorAll('h1, h2, h3, h4'));
  const marker = heads.find((h) => EVIDENCE_RE.test((h.textContent || '').trim()));
  if (!marker) return;
  const details = document.createElement('details');
  details.className = 'evidence-section';
  details.open = true;
  const summary = document.createElement('summary');
  const moved = [];
  for (let n = marker; n; n = n.nextElementSibling) moved.push(n);
  details.appendChild(summary);
  moved.forEach((n) => details.appendChild(n));
  const count = details.querySelectorAll('code.cite').length;
  summary.textContent = count
    ? `Evidence & validation · ${count} cited reference${count === 1 ? '' : 's'}`
    : 'Evidence & validation';
  bubble.appendChild(details);
}

async function refreshIndexStatus() {
  if (!indexStatus) return;
  try {
    const status = await fetchJson('/api/index-status');
    if (!status.available) {
      indexStatus.textContent = 'Index freshness unknown';
      return;
    }
    const when = status.generated_at ? new Date(status.generated_at) : null;
    indexStatus.textContent = when && !Number.isNaN(when.getTime())
      ? `Indexed ${when.toLocaleString()}`
      : 'Index metadata loaded';
    // Distinguish the two counts that used to be conflated: the mirror scan is a superset (incl.
    // non-system extras), the MDC product estate is the frozen set the views actually cover.
    const mirror = Number((status.repos || []).length);
    let title = `镜像扫描 ${mirror} 仓库`;
    try {
      const tags = await fetchJson('/repo-tags');  // same-origin via the proxy; the product estate
      const product = Number(tags.count || Object.keys(tags.repos || {}).length) || 0;
      if (product) title += ` · MDC 产品 ${product} 仓库`;
    } catch (e) { /* proxy/data not up — keep the mirror-only label */ }
    indexStatus.title = title;
  } catch (error) {
    indexStatus.textContent = 'Index status unavailable';
  }
}

// Whether the production-log track can actually run, checked BEFORE anyone clicks the log starter.
// No ?probe=1 here: probing opens connections to production systems, and this is page load.
async function refreshMcpStatus() {
  if (!mcpStatus) return;
  try {
    const status = await fetchJson('/api/mcp/status');
    const ready = (status.ready || []).length;
    const total = Number(status.operations || 0);
    const card = document.getElementById('prompt-logs');
    const tag = card && card.querySelector('.pc-tag');
    if (!status.calling_enabled) {
      mcpStatus.textContent = 'MCP off';
      mcpStatus.classList.add('is-off');
      mcpStatus.title = (status.calling_note
        || 'SDLC_MCP_ENABLED is unset — production log lookups will not run.')
        + '\n点开可以看有哪些 MCP 服务器和操作（看目录不需要开关打开）。';
      // Say it on the card too. The starter still works — the investigator will report that it did
      // NOT look — but during a demo you want to know that before you click it, not after.
      if (card) card.classList.remove('is-live');
      if (tag) tag.textContent = 'MCP 未开启';
      return;
    }
    mcpStatus.classList.remove('is-off');
    if (card) card.classList.add('is-live');
    if (tag) tag.textContent = 'MCP · LogDream';
    mcpStatus.textContent = `MCP ${ready}/${total}`;
    mcpStatus.title = `${ready} of ${total} declared operations are wired and callable`
      + (status.config_error ? ` · config error: ${status.config_error}` : '')
      + '\n点开看每个 MCP 是做什么的、可以手动调用一次，或者交给 AI 去查。';
  } catch (error) {
    mcpStatus.textContent = 'MCP unknown';
    mcpStatus.classList.add('is-off');
    mcpStatus.title = 'Could not read /api/mcp/status';
  }
}

// ---- the MCP catalog panel -------------------------------------------------------------------
// Opened from the status pill. The listing half reads /api/mcp/catalog, which touches only config,
// so this panel opens and is fully readable with calling switched off — seeing what the integration
// IS should never require the ability to fire it.
//
// Everything rendered from the server here is escaped and inserted as text. That matters more than
// usual for one field: `remote_description` comes off THEIR tools/list. It is worth showing (nobody
// documents a tool better than its owner) but it is text from a system we do not control, so it is
// escaped, labelled as theirs, and never composed into anything sent to the model.
const mcpPanel = document.getElementById('mcp-panel');
let mcpCatalog = null;
let mcpLastResult = null;

const MCP_STATE_LABEL = {
  ready: ['ok', '已接通'],
  partial: ['warn', '部分参数未填'],
  unwired: ['warn', '未接通'],
  disabled: ['', '服务器未启用'],
  blocked: ['bad', '硬性禁止'],
};

function mcpBadge(kind, text, title) {
  return `<span class="mcp-badge${kind ? ' ' + kind : ''}"${title ? ` title="${escapeAttribute(title)}"` : ''}>${escapeHtml(text)}</span>`;
}

function mcpStateBadge(state) {
  const [kind, label] = MCP_STATE_LABEL[state] || ['', state || 'unknown'];
  return mcpBadge(kind, label);
}

function renderMcpOperation(op) {
  const canCall = op.callable && mcpCatalog.calling_enabled && mcpCatalog.console_enabled;
  // Why the button is off, said once and specifically. "Disabled with no reason" is the single most
  // common way an internal tool wastes somebody's afternoon.
  let blockedReason = '';
  if (!mcpCatalog.calling_enabled) blockedReason = 'SDLC_MCP_ENABLED 未开启，这里不会发出任何请求。';
  else if (!mcpCatalog.console_enabled) blockedReason = 'SDLC_MCP_CONSOLE=0，只能查看不能手动调用（聊天里的调查员仍可用）。';
  else if (op.state === 'blocked') blockedReason = '这个工具在 never_expose 硬名单上，任何路径都不可调用。';
  else if (op.state === 'disabled') blockedReason = `服务器 ${op.server} 在配置里 enabled=false。`;
  else if (op.state === 'unwired') blockedReason = '配置里还没填工具名（tool 仍是 "?"）。';

  const args = (op.args || []).map((arg) => {
    const hint = arg.wired ? arg.their_name : '参数名未填（"?"）';
    return `<label for="mcparg-${escapeAttribute(op.operation)}-${escapeAttribute(arg.name)}">`
      + `${escapeHtml(arg.name)}<span>${escapeHtml(hint)}</span></label>`
      + `<input class="mcp-arg-input" type="text" data-arg="${escapeAttribute(arg.name)}"`
      + ` id="mcparg-${escapeAttribute(op.operation)}-${escapeAttribute(arg.name)}"`
      + ` placeholder="${escapeAttribute(arg.wired ? '留空则不发送这个参数' : '未接通')}"`
      + `${arg.wired ? '' : ' disabled'}>`;
  }).join('');

  return `<details class="mcp-op" data-operation="${escapeAttribute(op.operation)}">
    <summary>
      <span class="mcp-op-name">${escapeHtml(op.operation)}</span>
      ${mcpStateBadge(op.state)}
      ${op.data_class === 'payload'
        ? mcpBadge('payload', '可能含正文', '这类返回预期会带生产正文/客户可关联数据。注意：脱敏对所有操作一律执行，这个标记只决定提示力度。')
        : mcpBadge('', '元数据')}
      ${op.caller_policy === 'manual_only'
        ? mcpBadge('warn', '仅人工诊断', '已映射，可以在这里手动调，但产品的调查链永不调用它 —— 这条在引擎里是硬拦截，不是 UI 文案。')
        : op.caller_policy === 'disabled'
          ? mcpBadge('bad', '禁止调用', '保留映射只为让工具名可以和 tools/list 对账；任何路径都不可调用。')
          : ''}
      ${(op.semantic_warnings || []).length
        ? mcpBadge('bad', '语义告警', '远端在某些情况下会回答一个不同的问题：'
            + (op.semantic_warnings || []).join('、'))
        : ''}
      ${op.tool ? `<span class="mcp-op-tool">→ ${escapeHtml(op.tool)}</span>` : ''}
    </summary>
    <div class="mcp-op-body">
      ${op.purpose ? `<p>${escapeHtml(op.purpose)}</p>` : ''}
      ${op.remote_description
        ? `<p class="mcp-note">对方 tools/list 的说明（他们写的，原样转载）：${escapeHtml(op.remote_description)}</p>`
        : ''}
      ${(op.semantic_warnings || []).length
        ? `<p class="mcp-note"><b>语义告警</b>：${escapeHtml((op.semantic_warnings || []).join('、'))}`
          + `。远端在这些情况下会回答一个不同的问题，所以引擎会在本地复核返回，`
          + `复核不过的一律不算证据。</p>`
        : ''}
      ${op.caller_policy === 'manual_only'
        ? `<p class="mcp-note"><b>仅人工诊断</b>：调查链不会调用它。你可以在这里手动调、自己读，`
          + `但它的返回不会进入任何证据包 —— 这条是引擎里的硬拦截。</p>`
        : ''}
      ${op.note ? `<p class="mcp-note">${escapeHtml(op.note)}</p>` : ''}
      ${op.const_keys && op.const_keys.length
        ? `<p class="mcp-note">固定参数（每次调用自动带上，不可改）：${escapeHtml(op.const_keys.join(', '))}</p>`
        : ''}
      ${args ? `<div class="mcp-args">${args}</div>` : '<p class="mcp-note">这个操作不带参数。</p>'}
      <div class="mcp-op-actions">
        <button class="mcp-run" type="button"${canCall ? '' : ' disabled'}>调用</button>
        <button class="mcp-ask" type="button">让 AI 用这条去查</button>
      </div>
      ${blockedReason ? `<p class="mcp-result-note">${escapeHtml(blockedReason)}</p>` : ''}
      <div class="mcp-op-result"></div>
    </div>
  </details>`;
}

function renderMcpCatalog(data) {
  mcpCatalog = data;
  const body = mcpPanel.querySelector('.mcp-body');
  if (data.error) {
    body.innerHTML = `<div class="mcp-state is-off">读不到 MCP 目录：${escapeHtml(data.error)}</div>`;
    return;
  }
  const ops = data.operations || {};
  const total = Object.keys(ops).length;
  const layers = data.layers || {};
  const ready = layers.wired != null
    ? layers.wired : Object.values(ops).filter((op) => op.state === 'ready').length;
  const off = !data.calling_enabled || !data.console_enabled;
  const retention = data.raw_retention || {};

  // Three separate questions, because one ratio answered only the first and was read as all three
  // (intranet, 2026-08-04). "Wired" = names and arguments mapped. "Caller" = the product actually
  // uses it. "Evidence-safe" = and it has no known way of answering a different question than the
  // one asked. `log.read` is wired and called, and is NOT evidence-safe.
  const state = `<div class="mcp-state${off ? ' is-off' : ''}">`
    + `接线 <b>${ready}/${total}</b>`
    + (layers.caller != null ? ` · 产品已接 <b>${layers.caller}/${total}</b>` : '')
    + (layers.evidence_safe != null ? ` · 证据可信 <b>${layers.evidence_safe}/${total}</b>` : '')
    + `<br>调用开关 <b>${data.calling_enabled ? '开' : '关'}</b>`
    + ` · 手动调用 <b>${data.console_enabled ? '开' : '关'}</b>`
    + (layers.manual_only ? ` · 仅人工诊断 <b>${layers.manual_only}</b>` : '')
    + (layers.with_warnings ? ` · <b>${layers.with_warnings}</b> 条有语义告警` : '')
    + '<br><i>接线 ≠ 可信：接线只证明工具名和参数名对上了，不证明远端遵守参数语义。</i>'
    + (data.calling_note ? `<br>${escapeHtml(data.calling_note)}` : '')
    + (data.config_error ? `<br>配置读取失败：${escapeHtml(data.config_error)}` : '')
    + `<br>配置文件：<code>${escapeHtml(data.config_path || '')}</code>（工具名/参数名/返回结构都由内网在这里维护，改配置不用改代码）`
    + (retention.enabled
      ? '<br>⚠ 原文留存已开启（UAT 内测）：手动调用的未脱敏返回会存进按会话隔离的侧存储，可点「查看原文」核对。'
      : '<br>原文留存已关闭：手动调用的返回脱敏后即丢弃，没有任何接口能取回原文。')
    + '</div>';

  const servers = Object.values(data.servers || {}).map((server) => {
    const members = (server.operations || []).map((name) => ops[name]).filter(Boolean);
    return `<section class="mcp-server">
      <div class="mcp-server-head">
        <h3>${escapeHtml(server.name)}
          ${server.enabled ? mcpBadge('ok', 'enabled') : mcpBadge('warn', 'disabled')}
          ${server.transport ? mcpBadge('', server.transport) : mcpBadge('warn', 'transport 未定')}
          ${server.endpoint_configured
            ? mcpBadge('ok', '地址已配置')
            : mcpBadge('warn', `缺 ${server.url_env || 'url_env'}`, '地址只走环境变量，永不进 git')}
          ${mcpBadge('', `${server.ready}/${members.length} 就绪`)}
        </h3>
        ${server.purpose ? `<p class="mcp-server-purpose">${escapeHtml(server.purpose)}</p>` : ''}
      </div>
      ${members.map(renderMcpOperation).join('')}
    </section>`;
  }).join('');

  // The natural-language half. Deliberately routes into the EXISTING chat rather than driving MCP
  // itself: the agent reaches these servers only through the isolated investigator, which is what
  // keeps raw production text out of chat history. A second, panel-local agent loop would reopen
  // exactly that.
  const askBox = `<div class="mcp-ask-box">
    <h3>用自然语言问</h3>
    <p>写你想查什么，交给聊天里的 AI 去规划和调用（它通过隔离的调查员访问这几台服务器，
       返回的是脱敏后的证据包）。手动调用适合验证单个参数，自然语言适合查一件事。</p>
    <textarea class="mcp-ask-input" id="mcp-ask-input" placeholder="例如：mc-hk-hase-batch-letter-postman-job 在 2026-07-30 03:15 HKT 前后的日志说明了什么？"></textarea>
    <div class="mcp-op-actions" style="margin-top:8px"><button class="mcp-ask" id="mcp-ask-send" type="button">发送到聊天</button></div>
  </div>`;

  body.innerHTML = state + servers + askBox;
}

async function openMcpPanel() {
  usagePanel.hidden = true;
  llmPanel.hidden = true;
  mcpPanel.hidden = !mcpPanel.hidden;
  if (mcpPanel.hidden) return;
  mcpPanel.querySelector('.mcp-body').textContent = 'Loading...';
  try {
    renderMcpCatalog(await fetchJson('/api/mcp/catalog'));
  } catch (error) {
    mcpPanel.querySelector('.mcp-body').innerHTML =
      `<div class="mcp-state is-off">读不到 MCP 目录：${escapeHtml(error.message)}</div>`;
  }
}

function mcpResultBadges(result) {
  const parts = [];
  if (result.server) parts.push(mcpBadge('', result.server));
  if (result.tool) parts.push(mcpBadge('', result.tool));
  if (result.elapsed_ms) parts.push(mcpBadge('', `${result.elapsed_ms} ms`));
  if (result.retried) parts.push(mcpBadge('warn', `重试 ${result.attempts} 次`));
  if (result.truncated) parts.push(mcpBadge('warn', '已截断'));
  const redacted = Object.entries(result.redacted || {});
  if (redacted.length) {
    parts.push(mcpBadge('', '已脱敏 ' + redacted.map(([k, v]) => `${k}×${v}`).join(' ')));
  }
  const leaked = (result.exit_scan || {}).sanitized_at_exit || 0;
  if (leaked) parts.push(mcpBadge('bad', `出口再脱敏 ${leaked} 处`, '到出口还匹配 PII 说明上游脱敏有漏，请报告'));
  // The verdict, not just the response. A console that showed the lines while the product caller
  // consumed them as a keyword hit is the split this exists to close.
  const read = result.read_semantics;
  if (read) {
    if (read.semantic_downgrade) {
      parts.push(mcpBadge('bad', `降级成 ${read.actual_method || 'tail'}`,
        '关键词没命中，服务端回了文件尾部。这些行不是关键词证据。'));
    } else if (read.actual_method) {
      parts.push(mcpBadge('', `读法 ${read.actual_method}`));
    }
    parts.push(mcpBadge(read.evidence_accepted ? 'ok' : 'warn',
      `本地确认 ${read.literal_matches}/${read.lines_returned} 行`,
      '本地逐行复核了关键词是否真的出现在返回里 —— 这一步不依赖对方的字段和说法。'));
  }
  return parts.join('');
}

function renderMcpResult(host, result) {
  mcpLastResult = result;
  if (!result.called) {
    host.innerHTML = `<div class="mcp-result is-bad"><p class="mcp-result-note">`
      + `没有发出请求。${escapeHtml(result.error || '')}</p></div>`;
    return;
  }
  if (result.transport_failure) {
    host.innerHTML = `<div class="mcp-result is-bad">${mcpResultBadges(result)}`
      + `<p class="mcp-result-note">请求发不出去（${escapeHtml(result.kind || 'unreachable')}）：`
      + `${escapeHtml(result.error || '')}<br>`
      + '注意区分：这是「我们没问到」，不是「那边没有数据」。</p></div>';
    return;
  }
  // Their tool answering with an error is a different fact from a transport failure and from an
  // empty result; collapsing the three is how "the query was rejected" becomes "the log was clean".
  const bad = !result.ok;
  const shape = result.shape || {};
  // describe_shape returns a NESTED object for a JSON body (field names -> types, never values) and
  // a plain sentence for a text one. Rendering it with String() turned the useful case into
  // "[object Object]" — which is the one case the operator opened this panel for.
  const shapeText = typeof shape.shape === 'string'
    ? shape.shape : JSON.stringify(shape.shape || {}, null, 1);
  const parsed = shape.parsed
    ? '解析结果：' + escapeHtml(JSON.stringify(shape.parsed)) + '<br>' : '';
  const bodyText = result.text
    || (result.structured != null ? JSON.stringify(result.structured, null, 2) : '');
  host.innerHTML = `<div class="mcp-result${bad ? ' is-bad' : ''}">
    <div class="mcp-result-meta">${mcpResultBadges(result)}
      ${bad ? mcpBadge('bad', '对方工具报错') : mcpBadge('ok', 'ok')}</div>
    ${bodyText ? `<pre>${escapeHtml(bodyText)}</pre>` : '<p class="mcp-result-note">返回是空的。</p>'}
    <p class="mcp-result-note">
      发出的参数：${escapeHtml((result.params_sent || []).join(', ') || '（无）')}<br>
      返回结构${shape.body_is_json === false ? '（不是 JSON，走文本路径）' : ''}：<code>${escapeHtml(shapeText)}</code><br>
      ${parsed}${escapeHtml(result.storage_rule || '')}
    </p>
    <div class="mcp-op-actions" style="margin-top:8px">
      <button class="mcp-analyze" type="button">让 AI 分析这次结果</button>
      ${result.raw_ref ? `<button class="mcp-ask" type="button" data-raw-ref="${escapeAttribute(result.raw_ref)}">查看原文</button>` : ''}
    </div>
  </div>`;
}

async function runMcpOperation(details) {
  const operation = details.dataset.operation;
  const button = details.querySelector('.mcp-run');
  const host = details.querySelector('.mcp-op-result');
  const args = {};
  details.querySelectorAll('.mcp-arg-input').forEach((input) => {
    const value = input.value.trim();
    // An empty box means "do not send this argument", never "send an empty string". The whole
    // no-defaulting rule depends on an unsupplied argument staying unsupplied.
    if (value && !input.disabled) args[input.dataset.arg] = value;
  });
  button.disabled = true;
  host.innerHTML = '<p class="mcp-result-note">调用中…（一次读取通常 3–10 秒，日志类可能更久）</p>';
  try {
    const result = await fetchJson('/api/mcp/call', {
      method: 'POST', body: JSON.stringify({operation, args}),
    });
    renderMcpResult(host, result);
  } catch (error) {
    host.innerHTML = `<div class="mcp-result is-bad"><p class="mcp-result-note">${escapeHtml(error.message)}</p></div>`;
  } finally {
    button.disabled = false;
  }
}

function sendToChat(text) {
  if (!text.trim()) return;
  mcpPanel.hidden = true;
  q.value = text;
  f.requestSubmit();
}

// "Let the AI use this one" composes the question from OUR purpose text and the argument values the
// operator typed — never from the remote description. It is phrased as a question, not a tool call,
// because the agent reaches these servers through the investigator and picks the operations itself.
function askAboutOperation(details) {
  const op = (mcpCatalog.operations || {})[details.dataset.operation] || {};
  const typed = [];
  details.querySelectorAll('.mcp-arg-input').forEach((input) => {
    if (input.value.trim() && !input.disabled) typed.push(`${input.dataset.arg}=${input.value.trim()}`);
  });
  sendToChat(`我想用 MCP 查一件事：${op.purpose || op.operation}`
    + (typed.length ? `\n已知条件：${typed.join('，')}` : '')
    + '\n请你规划要读什么、实际去查，并说明每条结论的出处；查不到就说查不到，不要推测。');
}

// The redacted summary — the same class of text an evidence packet carries — is what goes into chat.
// Raw response text never does: chat history is replayed to the model every turn, so anything put
// there is re-read for the life of the conversation.
function analyzeMcpResult() {
  if (!mcpLastResult) return;
  const body = (mcpLastResult.text
    || (mcpLastResult.structured != null ? JSON.stringify(mcpLastResult.structured) : ''))
    .split('\n').slice(0, 40).join('\n').slice(0, 4000);
  sendToChat(`我手动调了 MCP 的 ${mcpLastResult.operation}（${mcpLastResult.server}/${mcpLastResult.tool}），`
    + `返回如下（已脱敏，<kind:xxxx> 是被替换掉的敏感值）：\n\n\`\`\`\n${body}\n\`\`\`\n\n`
    + '请解读这个结果：它说明了什么、没说明什么，以及下一步该查什么。'
    + '不要把脱敏标记当成真实值，也不要补充这段内容里没有的事实。');
}

// A probe opens a session to every enabled production server, so the server caches the result for
// MCP_PROBE_TTL. Clicking again while a CACHED answer is on screen means "no, actually reconnect" —
// which is the only reading of a second click, and the alternative (silently re-serving the same
// cached answer) would look like a broken button.
let mcpLastProbeWasCached = false;

async function probeMcpServers() {
  const button = document.getElementById('mcp-probe');
  const body = mcpPanel.querySelector('.mcp-body');
  button.disabled = true;
  const banner = document.createElement('div');
  banner.className = 'mcp-state';
  banner.textContent = mcpLastProbeWasCached
    ? '正在强制重新连接每台服务器…' : '正在连接每台服务器读 tools/list…';
  body.prepend(banner);
  try {
    const status = await fetchJson(
      '/api/mcp/status?probe=' + (mcpLastProbeWasCached ? 'fresh' : '1'));
    const probes = Object.values(status.probes || {});
    if (!probes.length) {
      banner.className = 'mcp-state is-off';
      banner.textContent = '没有已启用的服务器可以核对。';
      return;
    }
    // Say when an answer is a REUSED one and how old. "The names match" meaning "the names matched
    // a minute ago" is the same class of confusion this whole subsystem exists to prevent.
    mcpLastProbeWasCached = probes.some((probe) => probe.cached);
    banner.className = 'mcp-state';
    banner.innerHTML = probes.map((probe) => {
      const head = `<b>${escapeHtml(probe.server)}</b> — `
        + (probe.ok
          ? `声明的 ${probe.declared.length} 个工具名全部存在`
          : `<span style="color:#b91c1c">对不上：${escapeHtml((probe.missing || []).join(', ') || probe.reason || '')}</span>`)
        + (probe.cached ? `（${Math.round(probe.cached_age_seconds)} 秒前的结果，未重新连接）` : '');
      const unused = (probe.unused || []).length
        ? `<br>　他们还有我们没接的：${escapeHtml(probe.unused.join(', '))}` : '';
      return head + unused;
    }).join('<br>')
      + (mcpLastProbeWasCached ? '<br><i>再点一次「实时核对」会强制重新连接。</i>' : '');
    // Their own descriptions, merged into the operation rows now that we actually have them.
    const byTool = {};
    probes.forEach((probe) => (probe.details || []).forEach((detail) => {
      byTool[detail.name] = detail.description;
    }));
    Object.values(mcpCatalog.operations || {}).forEach((op) => {
      if (op.tool && byTool[op.tool]) op.remote_description = byTool[op.tool];
    });
    const previous = banner.innerHTML;
    renderMcpCatalog(mcpCatalog);
    const refreshed = document.createElement('div');
    refreshed.className = 'mcp-state';
    refreshed.innerHTML = previous;
    mcpPanel.querySelector('.mcp-body').prepend(refreshed);
  } catch (error) {
    banner.className = 'mcp-state is-off';
    banner.textContent = '核对失败：' + error.message;
  } finally {
    button.disabled = false;
  }
}

mcpStatus.addEventListener('click', openMcpPanel);
document.getElementById('mcp-probe').addEventListener('click', probeMcpServers);
mcpPanel.addEventListener('click', (event) => {
  const target = event.target;
  if (target.classList.contains('mcp-run')) runMcpOperation(target.closest('.mcp-op'));
  else if (target.classList.contains('mcp-ask') && target.closest('.mcp-op')) {
    if (target.dataset.rawRef) openRawLog(target.dataset.rawRef, 'MCP 手动调用原文');
    else askAboutOperation(target.closest('.mcp-op'));
  } else if (target.classList.contains('mcp-analyze')) analyzeMcpResult();
  else if (target.id === 'mcp-ask-send') sendToChat(document.getElementById('mcp-ask-input').value);
});

function usageMetric(label, value) {
  return `<div class="usage-metric"><span>${escapeHtml(label)}</span><strong>${formatCount(value)}</strong></div>`;
}

function renderUsageDashboard(data) {
  const body = usagePanel.querySelector('.usage-body');
  const total = data.total || {};
  const rows = (data.by_day || []).map((day) => (
    `<tr><td>${escapeHtml(day.date)}</td><td>${formatCount(day.total_tokens)}</td>`
    + `<td>${formatCount(day.total_nano_aiu)}</td><td>${formatCount(day.model_calls)}</td></tr>`
  )).join('');
  body.innerHTML = ''
    + '<div class="usage-grid">'
    + usageMetric('Sessions', data.session_count || 0)
    + usageMetric('Answers', total.answers || 0)
    + usageMetric('Tokens', total.total_tokens || 0)
    + usageMetric('Nano AIU', total.total_nano_aiu || 0)
    + '</div>'
    + '<table class="usage-days"><thead><tr><th>Date</th><th>Tokens</th><th>Nano AIU</th><th>Calls</th></tr></thead>'
    + `<tbody>${rows || '<tr><td colspan="4">No usage recorded yet</td></tr>'}</tbody></table>`
    + `<p class="usage-note">${escapeHtml(data.unit_note || '')}</p>`;
}

async function openUsageDashboard() {
  mcpPanel.hidden = true;                    // one drawer at a time; they share the same corner
  usagePanel.hidden = false;
  usagePanel.querySelector('.usage-body').textContent = 'Loading...';
  try {
    renderUsageDashboard(await fetchJson('/api/usage'));
  } catch (error) {
    usagePanel.querySelector('.usage-body').textContent = 'Failed: ' + error.message;
  }
}

function markCitations(bubble, report) {
  if (!bubble || !report) return;
  const status = {};
  (report.items || []).forEach((item) => {
    status[item.ref] = item;
  });

  bubble.querySelectorAll('code.cite').forEach((code) => {
    const ref = (code.textContent || '').trim();
    const item = status[ref];
    if (!item) return;
    code.classList.add(item.ok ? 'cite-ok' : 'cite-bad');
    if (!item.ok) code.title = 'Unverified: ' + (item.reason || 'citation not verified');
  });

  const total = Number(report.total) || 0;
  const verified = Number(report.verified) || 0;
  const chip = document.createElement('div');
  chip.className = 'cite-summary ' + (total && verified === total ? 'is-ok' : (total ? 'is-bad' : ''));
  chip.textContent = total
    ? `${verified}/${total} citations verified against source`
    : 'no citations to verify';

  const wrap = bubble.querySelector('.evidence-section') || bubble;
  wrap.appendChild(chip);
}

function parseCite(text) {
  const match = (text || '').trim().match(/^(.*?)(?::(\d+))?(?:-\d+)?$/);
  return match ? {path: match[1], line: match[2] ? Number(match[2]) : null} : null;
}

function closeSourcePanel() {
  document.getElementById('source-panel').hidden = true;
}

function closeRawLogPanel() {
  document.getElementById('rawlog-panel').hidden = true;
}

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  // Both drawers occupy the same edge of the screen, so only ever one is open. Close the top one.
  const rawlog = document.getElementById('rawlog-panel');
  if (!rawlog.hidden) closeRawLogPanel();
  else closeSourcePanel();
});

// Unredacted production log text, in the full-height drawer rather than the old inline pane (which
// lived inside the 260px step list — a scroll box inside a scroll box). Deliberately NOT given a
// "copy" button: the text is selectable, and a one-click "put production log lines on the clipboard"
// affordance is not something this UAT-only feature should add.
const rawLogWrapToggle = document.getElementById('rawlog-wrap');

async function openRawLog(ref, stepLabel) {
  if (!ref) return;
  const panel = document.getElementById('rawlog-panel');
  const title = panel.querySelector('.rawlog-title');
  const warn = panel.querySelector('.rawlog-warn');
  const body = panel.querySelector('.rawlog-body');

  closeSourcePanel();                       // one drawer at a time — they share the same edge
  panel.hidden = false;
  title.textContent = stepLabel || '生产日志原文';
  warn.textContent = '⚠ 未脱敏生产日志原文（UAT 内测）';
  body.className = 'rawlog-body' + (rawLogWrapToggle.checked ? ' is-wrapped' : '');
  body.innerHTML = '';
  const loading = document.createElement('div');
  loading.className = 'rawlog-note';
  loading.textContent = '读取中...';
  body.appendChild(loading);

  let data;
  try {
    const response = await fetch('/api/incident/raw?ref=' + encodeURIComponent(ref),
                                 {headers: authHeaders()});
    data = await response.json();
    if (!response.ok) {
      warn.textContent = '⚠ 取不到原文';
      loading.textContent = data.hint || data.error || `Request failed (${response.status})`;
      return;
    }
  } catch (error) {
    warn.textContent = '⚠ 取不到原文';
    loading.textContent = error.message;
    return;
  }

  warn.textContent = `⚠ 未脱敏生产日志原文 · 共 ${data.line_count} 行`
    + (data.truncated ? '（已截断）' : '') + ` · 存于 ${data.stored_at}`;
  body.innerHTML = '';
  (data.lines || []).forEach((line, index) => {
    const row = document.createElement('div');
    row.className = 'rawlog-line';
    const num = document.createElement('span');
    num.className = 'rawlog-n';
    num.textContent = index + 1;
    const text = document.createElement('span');
    text.className = 'rawlog-t';
    text.textContent = line;
    row.append(num, text);
    body.appendChild(row);
  });
  if (!(data.lines || []).length) {
    const note = document.createElement('div');
    note.className = 'rawlog-note';
    note.textContent = '这条证据没有留存任何原文行。';
    body.appendChild(note);
  }
}

rawLogWrapToggle.addEventListener('change', () => {
  document.querySelector('.rawlog-body')
    .classList.toggle('is-wrapped', rawLogWrapToggle.checked);
});

async function openSource(ref) {
  const citation = parseCite(ref);
  if (!citation || !citation.path) return;

  const panel = document.getElementById('source-panel');
  const title = panel.querySelector('.src-title');
  const body = panel.querySelector('.src-body');
  closeRawLogPanel();                       // one drawer at a time — they share the same edge
  panel.hidden = false;
  title.textContent = ref;
  body.textContent = 'Loading...';

  const url = '/api/source?path=' + encodeURIComponent(citation.path)
    + (citation.line ? '&line=' + encodeURIComponent(citation.line) : '');
  try {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok || data.error) {
      body.textContent = data.error || `Request failed (${response.status})`;
      return;
    }

    body.innerHTML = '';
    (data.lines || []).forEach((line) => {
      const row = document.createElement('div');
      row.className = 'src-line' + (line.n === data.line ? ' hit' : '');

      const num = document.createElement('span');
      num.className = 'src-n';
      num.textContent = line.n;

      const code = document.createElement('span');
      code.className = 'src-t';
      code.textContent = line.text || '';

      row.append(num, code);
      body.appendChild(row);
    });

    const hit = body.querySelector('.hit');
    if (hit) hit.scrollIntoView({block: 'center'});
  } catch (error) {
    body.textContent = 'Failed: ' + error.message;
  }
}

function renderMermaid(root) {
  // Only called on a completed message (never mid-stream), so the ```mermaid source is whole.
  if (!window.mermaid || !root) return;
  const nodes = Array.from(root.querySelectorAll('pre.mermaid:not([data-mermaid-done])'));
  if (!nodes.length) return;
  nodes.forEach((el) => el.setAttribute('data-mermaid-done', '1'));
  try {
    // mermaid v10+/v11: run() reads each node's text as the diagram source and swaps in the SVG.
    window.mermaid.run({ nodes });
  } catch (error) {
    // Bad syntax or a missing/old mermaid build -> leave the escaped source visible (no crash).
  }
}

function setBubbleContent(bubble, text, renderMarkdownContent = false) {
  if (renderMarkdownContent) {
    bubble.classList.add('markdown');
    bubble.innerHTML = renderMarkdown(text || '');
    decorateAnswer(bubble);
    renderMermaid(bubble);
    return;
  }

  bubble.classList.remove('markdown');
  bubble.textContent = text;
}

function add(role, text, who) {
  hideEmptyState();
  const m = document.createElement('div');
  m.className = 'msg ' + role;
  const label = document.createElement('div');
  label.className = 'who';
  label.textContent = who || role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  setBubbleContent(bubble, text, role === 'assistant');
  m.append(label, bubble);
  log.appendChild(m);
  log.scrollTop = log.scrollHeight;
  return m;
}

// ---------------------------------------------------------------------------------------------
// Retrieval steps. A chip used to carry the tool NAME and nothing else, which made every outcome
// look identical: a call that answered, a call that was rejected for a mistyped argument and a call
// that never happened all rendered as the same grey pill. Reconstructing which was which meant
// reading the answer text and guessing. Each chip is now a button over one ledger entry
// (webapp/tool_trace.py) and opens the input, the output, and — when it failed — WHICH argument and
// who can fix it.
//
// The output shown is the exact string the model was handed, not a second rendering of the result.
// One call, one account of it: two accounts is how a screen ends up blaming our own gate for a
// connection the peer refused.
const TOOL_STATUS_LABEL = {ok: '成功', error: '失败', running: '进行中',
                           unknown: '结果没有记录（升级前的旧记录）'};
const TOOL_STATUS_MARK = {ok: '✓', error: '✖', running: '⋯', unknown: '·'};

const FAILURE_LABEL = {
  bad_call_syntax: '参数不是合法 JSON',
  bad_arguments: '参数不对',
  empty_result: '结果为空',
  unavailable: '对端不可达',
  refused: '配置里的故意限制',
  duplicate: '这一轮已经调过',
  contract_only: '要走它自己的前置校验',
  internal_error: '我们这边出错'
};

// The three answers lead to three different next moves, which is the entire reason the field
// exists. `us` must never be phrased as something the reader can go and fill in — being sent to
// supply a fact that would not have helped is worse than being told nothing.
const WHO_CAN_CLOSE_LABEL = {
  assistant: '助手自己改参数再调一次（本轮就能修）',
  peer: '对端系统 / 网络 —— 不是"查不到数据"',
  config_owner: '配置拥有者（这是故意设的限制）',
  us: '我们 —— 这是我们的缺陷，不用你补任何东西',
  user: '需要你补一个我们拿不到的事实'
};

const ARG_PROBLEM_LABEL = {
  missing: '必填参数，这次没给',
  empty: '必填参数，给的是空字符串',
  wrong_type: '类型对不上，工具用不了 —— 这次没有真的调用',
  loose_type: '类型和 schema 不一致（仍按原值调用了，结果可能不是你以为的那个）',
  unknown_field: '工具没有这个参数，已被忽略',
  // 这个工具不在模型看得见的工具表里（十个旧名字仍然由 dispatch 路由给 CLI/MCP 用）。
  // 没有 schema 可以对照，所以这次的参数没有被校验 —— 说出来，不假装检查过。
  no_schema: '这个工具不在模型的工具表里，所以参数没有被校验'
};

function toolStatusOf(item) {
  if (item.status) return item.status;
  if (item.failure_class) return 'error';
  // A turn saved before this panel existed carries the tool name and nothing else. Calling that
  // "成功" would be an assertion nothing backs — the same shape of mistake as rendering an unknown
  // count as a zero.
  return item.output ? 'ok' : 'unknown';
}

function formatCount(value) {
  // `—` for unknown, never 0. "We do not know how big it was" and "it was empty" are different
  // facts, and rendering the first as the second is the exact bug this panel exists to stop.
  return (value == null) ? '—' : Number(value).toLocaleString('en-US');
}

function argValueText(value) {
  if (value === undefined) return '(未传)';
  try {
    return typeof value === 'string' ? value : JSON.stringify(value);
  } catch (error) {
    return String(value);
  }
}

function toolDetailNode(item, seq) {
  const wrap = document.createElement('div');
  wrap.className = 'tool-detail';
  const status = toolStatusOf(item);

  const head = document.createElement('div');
  head.className = 'td-head';
  const title = document.createElement('b');
  title.textContent = `#${seq} ${item.tool || 'tool'}`;
  const meta = document.createElement('span');
  const bits = [TOOL_STATUS_LABEL[status] || status];
  if (item.duration_ms != null) bits.push(`${formatCount(item.duration_ms)} ms`);
  if (item.iteration != null) bits.push(`第 ${item.iteration} 轮`);
  if (item.attempt != null) bits.push(`这个工具第 ${item.attempt} 次`);
  if (item.lane) bits.push(`预算 lane: ${item.lane}`);
  meta.textContent = bits.join(' · ');
  head.append(title, meta);
  wrap.appendChild(head);

  if (status === 'error') {
    const why = document.createElement('div');
    why.className = 'td-why';
    const rows = [
      ['为什么失败', FAILURE_LABEL[item.failure_class] || item.failure_class || '—'],
      // "never ran" and "ran and failed" are different facts. Collapsing them is how a panel ends
      // up blaming our own gate for a connection the peer refused.
      ['工具跑了吗', item.dispatched ? '跑了，是它自己报的失败' : '没跑 —— 这次调用在发出前就被拦下了'],
      ['工具自己说的', item.message || '—'],
      ['谁能解开', WHO_CAN_CLOSE_LABEL[item.who_can_close] || item.who_can_close || '—']
    ];
    rows.forEach(([label, value]) => {
      const row = document.createElement('div');
      const key = document.createElement('span');
      key.className = 'td-key';
      key.textContent = label;
      const val = document.createElement('span');
      val.textContent = value;
      row.append(key, val);
      why.appendChild(row);
    });
    wrap.appendChild(why);
  }

  // `field: "tool"` is about the CALL, not about an argument — listing it in the argument table
  // would invent a parameter nobody passed. It gets its own line instead.
  const problems = new Map();
  (item.invalid || []).concat(item.notes || []).forEach((entry) => {
    if (!entry || !entry.field || entry.field === 'tool') return;
    problems.set(entry.field, entry);
  });
  (item.notes || []).concat(item.invalid || []).forEach((entry) => {
    if (!entry || entry.field !== 'tool') return;
    const line = document.createElement('p');
    line.className = 'td-note-line';
    line.textContent = ARG_PROBLEM_LABEL[entry.problem] || entry.problem;
    wrap.appendChild(line);
  });

  const inputs = document.createElement('div');
  inputs.className = 'td-section';
  const inputTitle = document.createElement('h5');
  inputTitle.textContent = '输入参数';
  inputs.appendChild(inputTitle);

  const fields = new Set(Object.keys(item.args || {}));
  problems.forEach((_entry, field) => fields.add(field));
  if (!fields.size) {
    const none = document.createElement('p');
    none.className = 'td-none';
    none.textContent = '这次调用没有参数。';
    inputs.appendChild(none);
  } else {
    const table = document.createElement('table');
    table.className = 'td-args';
    fields.forEach((field) => {
      const entry = problems.get(field);
      const row = document.createElement('tr');
      if (entry) row.className = (item.invalid || []).includes(entry) ? 'is-invalid' : 'is-note';
      const name = document.createElement('th');
      name.textContent = field;
      const value = document.createElement('td');
      value.textContent = argValueText((item.args || {})[field]);
      row.append(name, value);
      table.appendChild(row);
      if (entry) {
        const note = document.createElement('tr');
        note.className = 'td-arg-note';
        const spacer = document.createElement('th');
        const text = document.createElement('td');
        // `expected` is copied from the tool schema. When the schema does not constrain a field it
        // says so — a made-up expectation is how a wrong argument becomes a confidently wrong one.
        text.textContent = `${ARG_PROBLEM_LABEL[entry.problem] || entry.problem}`
          + ` · schema 要求: ${entry.expected || '未知'} · 实际收到: ${entry.actual_type || '—'}`;
        note.append(spacer, text);
        table.appendChild(note);
      }
    });
    inputs.appendChild(table);
  }
  wrap.appendChild(inputs);

  if (item.arguments_raw != null) {
    const raw = document.createElement('div');
    raw.className = 'td-section';
    const rawTitle = document.createElement('h5');
    rawTitle.textContent = '模型原样发出的 arguments（没解析成功的就是这段）';
    const pre = document.createElement('pre');
    pre.className = 'td-pre';
    pre.textContent = item.arguments_raw;
    raw.append(rawTitle, pre);
    wrap.appendChild(raw);
  }

  const output = document.createElement('div');
  output.className = 'td-section';
  const outTitle = document.createElement('h5');
  outTitle.textContent = '输出（模型收到的就是这一段）';
  output.appendChild(outTitle);
  const box = item.output || {};
  const pre = document.createElement('pre');
  pre.className = 'td-pre';
  // Three different absences, said as three different things. A turn saved before this panel
  // existed has no output stored, and rendering that as "still running" or as an empty result
  // would be the same mistake the panel was built to stop.
  pre.textContent = box.text != null ? box.text
    : (toolStatusOf(item) === 'running' ? '（这一步还在跑，还没有输出）'
       : '（这条是旧记录：当时还没有保存输入/输出，只留下了工具名）');
  output.appendChild(pre);
  if (box.model_chars != null) {
    const size = document.createElement('p');
    size.className = 'td-size';
    size.textContent = `工具返回 ${formatCount(box.result_chars)} 字符 → 模型收到 `
      + `${formatCount(box.model_chars)} 字符（超出的部分由上下文预算按结构裁剪）→ `
      + `这里显示 ${formatCount(box.shown_chars)} 字符`;
    output.appendChild(size);
  }
  wrap.appendChild(output);
  return wrap;
}

function renderToolTrace(container, trace, completed = false) {
  const existing = container.querySelector('.tools');
  // Survive the re-render: the live stream redraws this block on every tool event, and an open
  // detail that closes itself mid-read is worse than no detail at all.
  const openSeq = existing ? existing.dataset.openSeq : '';
  if (existing) existing.remove();
  if (!trace || !trace.length) return;

  const details = document.createElement('details');
  details.className = 'tools';
  details.open = true;
  if (openSeq) details.dataset.openSeq = openSeq;

  const failed = trace.filter((item) => toolStatusOf(item) === 'error').length;
  const summary = document.createElement('summary');
  const label = document.createElement('span');
  label.textContent = `${trace.length} 个检索步骤${completed ? '' : '（进行中）'}`
    + (failed ? ` · ${failed} 个失败` : '');
  const hint = document.createElement('span');
  hint.className = 'tools-hint';
  hint.textContent = '点任意一步看输入 / 输出';
  summary.append(label, hint);

  const list = document.createElement('div');
  list.className = 'tools-list';
  const host = document.createElement('div');
  host.className = 'tool-detail-host';

  const show = (item, seq) => {
    host.replaceChildren(toolDetailNode(item, seq));
  };

  trace.forEach((item, index) => {
    const seq = item.seq == null ? index + 1 : item.seq;
    const status = toolStatusOf(item);
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = `tool-chip is-${status}`;
    chip.dataset.seq = String(seq);
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = TOOL_STATUS_MARK[status] || '·';
    chip.append(mark, document.createTextNode(item.tool || 'tool'));
    // The tool name goes in the title too: a screen reader announces the title over the chip's own
    // text, and "成功" on its own does not say WHICH call succeeded.
    chip.title = `${item.tool || 'tool'} — ${TOOL_STATUS_LABEL[status] || status}，点开看这次调用的输入和输出`;
    chip.addEventListener('click', () => {
      if (details.dataset.openSeq === String(seq)) {
        details.dataset.openSeq = '';
        host.replaceChildren();
        chip.classList.remove('is-open');
        return;
      }
      details.dataset.openSeq = String(seq);
      list.querySelectorAll('.tool-chip.is-open').forEach((other) => other.classList.remove('is-open'));
      chip.classList.add('is-open');
      show(item, seq);
    });
    if (String(seq) === openSeq) {
      chip.classList.add('is-open');
      show(item, seq);
    }
    list.appendChild(chip);
  });

  details.append(summary, list, host);
  container.appendChild(details);
}

// Kept as names because two call sites read as what they do: replaying a stored turn, and redrawing
// a live one. Both render the same ledger — there is only one renderer now.
function addToolTrace(container, trace) {
  renderToolTrace(container, trace, true);
}

function showLiveTools(container, trace, completed = false) {
  renderToolTrace(container, trace, completed);
  log.scrollTop = log.scrollHeight;
}

// Step kinds that must not read as routine progress. A server that did not answer, or a repo that
// was skipped, is a "we did not look" — the whole point of surfacing these is that the user can tell
// that apart from "we looked and found nothing".
const SUBAGENT_WARN = new Set(['budget_spent', 'query_failed', 'app_unresolved', 'query_empty',
                               'no_files', 'metric_window_empty']);
// `query_rejected` / `apps_failed` mean the tool RAN and refused — a different escalation from a
// server that did not answer, and emphatically not a finding. `query_unreadable` is a third thing
// again: the call SUCCEEDED and our own parser could not read the response shape. It escalates to
// us (a config/mcp_tools.json `response` mapping), never to the log team, and it must never look
// like an empty result.
const SUBAGENT_STOP = new Set(['refused', 'disabled', 'apps_failed', 'unwired', 'query_rejected',
                               'query_unreadable', 'alarm_lookup_failed', 'metric_window_failed']);
// The CloudWatch branch runs alongside the log branch and gets its own marks, so a reader can see
// at a glance which half a step belongs to. `metric_window_empty` is a WARNING, not a hit: no
// datapoint in a window is not the same as a healthy service.
const SUBAGENT_MARKS = {
  plan: '◇', plan_done: '◆', apps: '◇', apps_done: '◆', app_resolved: '→',
  app_unresolved: '⚠', search_files: '◇', files_found: '→', no_files: '⚠',
  query: '·', query_empty: '○', query_failed: '⚠', evidence: '●',
  query_rejected: '✖', budget_spent: '⚠', refused: '■', disabled: '■', apps_failed: '■',
  unwired: '■', query_unreadable: '✖', summary: '✓',
  alarm_resolve: '◇', alarm_lookup: '◇', alarm_lookup_failed: '✖',
  metric_window: '·', metric_window_empty: '○', metric_window_failed: '✖', metric_evidence: '●'
};

function showSubagent(container, steps, completed = false) {
  const existing = container.querySelector('.subagent');
  if (existing) existing.remove();
  if (!steps.length) return;

  const details = document.createElement('details');
  details.className = 'subagent';
  details.open = true;

  const summary = document.createElement('summary');
  const dot = document.createElement('span');
  if (completed) {
    dot.textContent = '✓';
  } else {
    dot.className = 'subagent-live';
  }
  const title = document.createElement('span');
  const hits = steps.filter((s) => s.step === 'evidence').length;
  title.textContent = completed
    ? `事故调查员 已完成 · ${steps.length} 步` + (hits ? ` · ${hits} 条证据` : '')
    : `事故调查员 正在调查 · ${steps.length} 步`;
  summary.append(dot, title);

  const list = document.createElement('div');
  list.className = 'subagent-steps';
  steps.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'subagent-step';
    if (SUBAGENT_STOP.has(item.step)) row.classList.add('is-stop');
    else if (SUBAGENT_WARN.has(item.step)) row.classList.add('is-warn');
    else if (item.step === 'evidence') row.classList.add('is-hit');
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = SUBAGENT_MARKS[item.step] || '·';
    row.appendChild(mark);

    const detail = item.detail || {};
    if (detail.server) {
      const badge = document.createElement('span');
      badge.className = 'subagent-mcp';
      // Name the MCP server AND the abstract operation. "logdream · log.read" tells an operator both
      // which system was contacted and which of our allow-listed operations did it.
      badge.textContent = detail.server + (detail.operation ? ' · ' + detail.operation : '');
      badge.title = detail.refused_locally
        ? '本地白名单/命名层拒绝，未发出网络请求'
        : '经 MCP 调用外部系统';
      row.appendChild(badge);
    }

    const label = document.createElement('span');
    label.textContent = item.label || item.step || '';
    row.appendChild(label);

    if (typeof detail.elapsed_ms === 'number') {
      const took = document.createElement('span');
      took.className = 'took' + (detail.elapsed_ms >= 5000 ? ' is-slow' : '');
      took.textContent = (detail.elapsed_ms / 1000).toFixed(1) + 's';
      row.appendChild(took);
    }
    list.appendChild(row);

    // Click-through to the original lines. Only present under raw retention; the ref is opaque and
    // the fetch is owner-scoped server-side, so the button cannot reach another session's logs.
    // Opens the full-height drawer — see openRawLog.
    if (detail.raw_ref) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'subagent-raw-btn';
      btn.textContent = '查看原文';
      btn.title = '未脱敏的生产日志原文（UAT 内测）';
      btn.addEventListener('click', () => openRawLog(detail.raw_ref, item.label || ''));
      row.appendChild(btn);
    }
  });

  const note = document.createElement('div');
  note.className = 'subagent-note';
  const mcpCount = steps.filter((s) => (s.detail || {}).server).length;
  const retained = steps.filter((s) => (s.detail || {}).raw_ref).length;
  note.textContent = (mcpCount ? `经 MCP 调用外部系统 ${mcpCount} 次。` : '')
    + (retained
        ? '模型只收到脱敏聚合结果；原文单独留存，仅本浏览器可点开核对。'
        : '原始生产日志只存在于调查员内存中，脱敏聚合后才返回；此处只显示计数与异常类，不显示日志内容。');

  details.append(summary, list);
  if (retained) {
    const banner = document.createElement('div');
    banner.className = 'subagent-retention';
    banner.textContent = `⚠ UAT 内测模式：本次 ${retained} 条证据的未脱敏日志原文已留存在服务器上，`
      + '可点「查看原文」核对。正式使用前必须关闭 SDLC_INCIDENT_RAW_LOGS。';
    details.appendChild(banner);
  }
  details.appendChild(note);
  container.appendChild(details);
  list.scrollTop = list.scrollHeight;
  log.scrollTop = log.scrollHeight;
}

function formatCount(value) {
  return (Number(value) || 0).toLocaleString('en-US');
}

function renderUsage(container, usage) {
  if (!usage) return;
  const totalTokens = Number(usage.total_tokens) || 0;
  const nanoAiu = Number(usage.total_nano_aiu) || 0;
  const calls = Array.isArray(usage.calls) ? usage.calls.length : 0;
  if (!totalTokens && !nanoAiu && !calls) return;
  const row = document.createElement('div');
  row.className = 'usage-row';
  row.style.cssText = 'margin-top:6px;color:var(--muted);font-size:12px;'
    + 'font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;';
  row.textContent = 'Usage: ' + formatCount(usage.total_tokens) + ' tokens'
    + ' · input ' + formatCount(usage.input_tokens)
    + ' · output ' + formatCount(usage.output_tokens)
    + ' · reasoning ' + formatCount(usage.reasoning_tokens)
    + ' · ' + formatCount(usage.total_nano_aiu) + ' nano AIU'
    + (calls > 1 ? ' · ' + calls + ' model calls' : '');
  container.appendChild(row);
}

function submitFeedback(sessionId, messageIndex, vote, comment) {
  return fetchJson('/api/feedback', {
    method: 'POST',
    body: JSON.stringify({session_id: sessionId, message_index: messageIndex, vote, comment: comment || ''})
  });
}

// 👍/👎 on an assistant answer. A 👎 reveals a comment box; the vote is recorded immediately and
// the comment (optional) is what future prompt/tool tuning learns from. `existing` restores the
// prior vote/comment when a saved session is reloaded. Addressed by (session id, message index).
function renderFeedback(container, sessionId, messageIndex, existing) {
  if (!container || !sessionId || messageIndex == null || messageIndex < 0) return;
  const prior = container.querySelector('.feedback-bar');
  if (prior) prior.remove();
  const priorComment = container.querySelector('.fb-comment');
  if (priorComment) priorComment.remove();

  const bar = document.createElement('div');
  bar.className = 'feedback-bar';
  const up = document.createElement('button');
  up.type = 'button'; up.className = 'fb-btn fb-up'; up.title = 'Helpful'; up.textContent = '👍';
  const down = document.createElement('button');
  down.type = 'button'; down.className = 'fb-btn fb-down'; down.title = 'Not helpful — tell us why'; down.textContent = '👎';
  const note = document.createElement('span');
  note.className = 'fb-note';

  const commentWrap = document.createElement('div');
  commentWrap.className = 'fb-comment';
  commentWrap.hidden = true;
  const input = document.createElement('textarea');
  input.className = 'fb-comment-input';
  input.placeholder = 'What was wrong or missing? (optional — this is what we use to improve)';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button'; saveBtn.className = 'fb-comment-save'; saveBtn.textContent = 'Send feedback';
  commentWrap.append(input, saveBtn);

  function reflect(vote) {
    up.classList.toggle('active', vote === 'up');
    down.classList.toggle('active', vote === 'down');
  }

  up.addEventListener('click', async () => {
    reflect('up');
    commentWrap.hidden = true;
    try { await submitFeedback(sessionId, messageIndex, 'up', ''); note.textContent = 'Thanks!'; }
    catch (e) { note.textContent = 'Failed: ' + e.message; }
  });
  down.addEventListener('click', async () => {
    reflect('down');
    commentWrap.hidden = false;
    input.focus();
    try { await submitFeedback(sessionId, messageIndex, 'down', input.value); note.textContent = ''; }
    catch (e) { note.textContent = 'Failed: ' + e.message; }
  });
  saveBtn.addEventListener('click', async () => {
    try { await submitFeedback(sessionId, messageIndex, 'down', input.value); note.textContent = 'Thanks — recorded.'; }
    catch (e) { note.textContent = 'Failed: ' + e.message; }
  });

  bar.append(up, down, note);
  container.append(bar, commentWrap);

  if (existing && existing.vote) {
    reflect(existing.vote);
    if (existing.vote === 'down') {
      commentWrap.hidden = false;
      input.value = existing.comment || '';
    }
  }
}

async function ask(text) {
  add('user', text, 'you');
  history.push({role:'user', content:text});
  const pending = add('assistant', 'Thinking through the relevant retrieval steps...', 'assistant');
  pending.querySelector('.bubble').classList.add('loading');
  setBusy(true);
  try {
    const r = await fetch('/api/chat', {method:'POST',
      headers:{'Content-Type':'application/json', ...authHeaders()},
      body: JSON.stringify({question:text, session_id: currentSessionId})});
    const d = await r.json();
    pending.querySelector('.bubble').classList.remove('loading');
    if (d.error) {
      setBubbleContent(pending.querySelector('.bubble'), 'Error: ' + d.error, false);
      if (d.reconnect_required || r.status === 401 || r.status === 403) await refreshLlmStatus();
    } else {
      const bubble = pending.querySelector('.bubble');
      setBubbleContent(bubble, d.answer || '(empty)', true);
      markCitations(bubble, d.citations);
      history.push({role:'assistant', content:d.answer || ''});
      if (d.views && d.views.length) d.views.forEach((v) => renderInlineView(pending, v));
      if (d.tool_trace && d.tool_trace.length) addToolTrace(pending, d.tool_trace);
      if (d.usage) renderUsage(pending, d.usage);
      if (d.session) {
        currentSessionId = d.session.id;
        await refreshSessions(currentSessionId);
        renderFeedback(pending, d.session.id, (d.session.message_count || 0) - 1, null);
      }
    }
  } catch (e) {
    pending.querySelector('.bubble').classList.remove('loading');
    setBubbleContent(pending.querySelector('.bubble'), 'Request failed: ' + e, false);
  }
  setBusy(false);
  q.focus();
}

// Retrieval service (serves arch.html + its data) — same host, port 8848 by convention.
// Same-origin by default: the chat server now reverse-proxies the arch/impact/coverage pages, so
// inline views load from this one port (no separate :8848 to reach or hardcode). Override only if
// you deliberately point the frontend at a standalone retrieval host.
const RETRIEVAL_BASE = (window.RETRIEVAL_BASE != null) ? window.RETRIEVAL_BASE : '';

// Inline architecture view: the assistant embeds the highlighted diagram straight into its answer,
// so the user never opens a page or clicks a node. Appended to the message CONTAINER (not the text
// bubble, which streaming overwrites).
const REL_LABELS = {
  'channel-owner': '拥有渠道', 'serves-channel': '波及(库级)', 'msg-channel': '消息连接',
  'delivery-job': '投递任务', 'outbound-api': '供应商 API',
  'dependency-upstream': '上游依赖', 'dependency-downstream': '下游依赖'
};

function inlineImpactHtml(impact) {
  if (!impact) return '';
  const uc = impact.use_cases || {}, rp = impact.repos || {};
  const chips = (arr) => (arr || []).map((x) => '<span class="ichip">' + escapeHtml(x) + '</span>').join('');
  const relChips = Object.entries(rp.by_relation || {})
    .map(([k, v]) => '<span class="ichip">' + escapeHtml(REL_LABELS[k] || k) + ' · ' + v + '</span>').join('');
  const rows = [];
  rows.push('<div class="ii-row"><b>受影响用例 ' + (uc.count || 0) + '</b>' + chips(uc.items) + '</div>');
  rows.push('<div class="ii-row"><b>受影响仓库 ' + (rp.count || 0) + '</b>' + relChips + '</div>');
  if (rp.sample && rp.sample.length) rows.push('<div class="ii-row ii-repos">' + chips(rp.sample) + '</div>');
  return '<div class="inline-impact">' + rows.join('') + '</div>';
}

// ---------------------------------------------------------------------------------------------
// Channel tiers (RUNBOOK-77/78). The point of this block is that four different facts used to
// arrive as the same word. "This repo sends SMS" and "this repo would be affected if SMS broke"
// are not the same claim, and a notification list built from the second is a list of teams who did
// not need telling — while the ones who did are missing. So every channel here states WHY, and the
// weakest reason (a channel word in the repo NAME) is styled as the weakest thing on the card
// rather than looking identical to a cited line of source.
const CH_RELATION = {
  direct_code_evidence:  {badge: '代码证据', cls: 'ev-code',    why: '它的源码在做这件事'},
  direct_config_evidence:{badge: '配置证据', cls: 'ev-config',  why: '它的配置在做这件事'},
  business_declared:     {badge: '业务声明', cls: 'ev-declared',why: '业务表里声明的'},
  name_derived:          {badge: '名称推断', cls: 'ev-name',    why: '仅凭仓库名里有这个词 —— 没有别的依据'},
  message_carried:       {badge: '消息链路', cls: 'ev-msg',     why: '它接触的 topic 携带这个渠道'},
  transitive_dependency: {badge: '依赖传播', cls: 'ev-trans',   why: '它会被连累 —— 它自己不发这个渠道'}
};

function chBadge(relation, confidence) {
  const meta = CH_RELATION[relation] || {badge: relation, cls: 'ev-name'};
  let html = '<span class="ev-badge ' + meta.cls + '">' + escapeHtml(meta.badge) + '</span>';
  // Low confidence gets its OWN badge rather than a lighter shade of the same one: colour alone is
  // not a label, and "code evidence" at low confidence must not read as "code evidence".
  if (confidence === 'low') html += '<span class="ev-badge ev-low">低置信</span>';
  return html;
}

function chEvidenceRows(row) {
  if (!row.evidence || !row.evidence.length) return '';
  const rows = row.evidence.map((item) => (
    '<div class="ev-row">' +
      '<code class="cite">' + escapeHtml(item.citation || '') + '</code>' +
      '<span class="ev-row-meta">' + escapeHtml(item.basis || '') + ' · ' +
        escapeHtml(item.confidence || '') + '</span>' +
    '</div>'
  )).join('');
  // <details> rather than always-open: the default answer stays short, and the audit trail is one
  // click away instead of burying the conclusion under citations.
  return '<details class="ev-drawer"><summary>证据 ' + row.evidence.length + ' 条（点引用看源码）</summary>' +
         rows + '</details>';
}

function chCard(row) {
  const meta = CH_RELATION[row.relation] || {why: row.relation};
  const kind = row.direct
    ? '<span class="ch-kind ch-direct">直接</span>'
    : '<span class="ch-kind ch-indirect">间接</span>';
  const also = (row.relations || []).slice(1)
    .map((extra) => chBadge(extra.relation, extra.confidence)).join('');
  return '<div class="ch-card' + (row.direct ? '' : ' ch-card-indirect') + '">' +
    '<div class="ch-head">' +
      '<span class="ch-name">' + escapeHtml((row.channel || '').toUpperCase()) + '</span>' +
      kind + chBadge(row.relation, row.confidence) +
    '</div>' +
    '<div class="ch-why">' + escapeHtml(meta.why || '') + '</div>' +
    (also ? '<div class="ch-also">也有：' + also + '</div>' : '') +
    chEvidenceRows(row) +
  '</div>';
}

function chCoverage(block) {
  const down = block.downstream || {};
  const kinds = down.unknown_breakdown || {};
  const bits = [];
  if (block.evidence_available === false) {
    bits.push('<div class="ch-warn">没有可用的证据文件 —— 下面只有名称/业务表/依赖图这三层，' +
              '代码层是空的（不是"没有证据"，是没查过）。</div>');
  }
  if (!block.scope_known) {
    // The unflattering reading, on purpose: the output is a notification list somebody acts on.
    bits.push('<div class="ch-warn">没有扫描范围文件 —— 无法区分「查过了确实干净」和「压根没查」，' +
              '所以下面的未知数不能收紧。</div>');
  }
  if (down.unknown_repos) {
    let line = '下游 ' + down.unknown_repos + ' 个仓库没有自己的渠道 —— 上面的渠道分布是<b>下界</b>';
    if (block.scope_known) {
      // "查过没发现" and "范围外" are both CONDITIONAL counts (no other layer either), so they are
      // worded as subsets. The intranet caught the earlier wording quoting the conditional number
      // as if it were the population — an undercount of what was never looked at, phrased as a
      // report of it.
      line += '（其中<b>查过没发现</b> ' + (kinds.scanned_clean || 0) + ' 个；' +
              '<b>在本轮扫描范围外、且没有其他线索</b> ' + (kinds.out_of_scope || 0) +
              ' 个 —— <b>范围外不等于没有渠道</b>）';
    }
    bits.push('<div class="ch-cov">' + line + '</div>');
  }
  if (block.scanned_without_evidence) {
    bits.push('<div class="ch-cov">全库另有 ' + block.scanned_without_evidence +
              ' 个仓库<b>扫过、读了源码和配置、没找到渠道标记</b> —— 这是一个结论,不是未知。</div>');
  }
  return bits.join('');
}

function inlineChannelsHtml(block) {
  if (!block || (!block.own && !block.downstream)) return '';
  const own = block.own || [];
  const direct = own.filter((row) => row.direct);
  const indirect = own.filter((row) => !row.direct);
  const parts = [];

  parts.push('<div class="ch-summary">' +
    '<b>它自己处理：</b>' + (direct.length
      ? direct.map((r) => escapeHtml(r.channel.toUpperCase())).join('、')
      : '<span class="ch-none">无（或未知）</span>') +
    ' <span class="ch-sep">·</span> <b>可能受影响：</b>' + (own.length
      ? own.map((r) => escapeHtml(r.channel.toUpperCase())).join('、')
      : '<span class="ch-none">未知</span>') +
  '</div>');

  if (direct.length) parts.push(direct.map(chCard).join(''));
  // The long tail collapses. A shared library reaches most channels indirectly, and rendering ten
  // identical "would be affected" cards buries the two that are actually its own.
  if (indirect.length) {
    parts.push('<details class="ch-tail"><summary>间接受影响的渠道 ' + indirect.length +
               ' 个（它自己不发这些）</summary>' + indirect.map(chCard).join('') + '</details>');
  }

  const spread = (block.downstream || {}).channels || [];
  if (spread.length) {
    const rows = spread.map((item) => {
      const meta = CH_RELATION[item.strongest_relation] || {badge: item.strongest_relation};
      const backed = item.code_backed ? '，其中 ' + item.code_backed + ' 个有代码/配置证据' : '';
      return '<div class="ch-spread-row"><span class="ch-name">' +
        escapeHtml(item.channel.toUpperCase()) + '</span> ' + item.repos + ' 个仓库' +
        escapeHtml(backed) + ' <span class="ev-badge ' + (meta.cls || 'ev-name') + '">最强：' +
        escapeHtml(meta.badge || '') + '</span></div>';
    }).join('');
    parts.push('<div class="ch-spread"><div class="ch-spread-cap">下游仓库的渠道分布</div>' +
               rows + '</div>');
  }

  parts.push(chCoverage(block));
  return '<div class="inline-channels">' + parts.join('') + '</div>';
}

function renderInlineView(container, view) {
  if (!container || !view || !view.url) return;
  if (container.querySelector('.inline-view[data-url="' + view.url + '"]')) return;  // no dupes
  const wrap = document.createElement('div');
  wrap.className = 'inline-view';
  wrap.setAttribute('data-url', view.url);
  const cap = document.createElement('div');
  cap.className = 'inline-view-cap';
  cap.textContent = view.summary || '架构图 · 受影响链路';
  const frame = document.createElement('iframe');
  frame.className = 'inline-view-frame';
  frame.src = RETRIEVAL_BASE + view.url;
  frame.setAttribute('loading', 'lazy');
  frame.setAttribute('title', 'architecture diagram');
  wrap.appendChild(cap);
  wrap.appendChild(frame);
  if (view.impact) wrap.insertAdjacentHTML('beforeend', inlineImpactHtml(view.impact));
  if (view.channels) wrap.insertAdjacentHTML('beforeend', inlineChannelsHtml(view.channels));
  container.appendChild(wrap);
  if (typeof log !== 'undefined' && log) log.scrollTop = log.scrollHeight;
}

// The embedded arch page reports its rendered height so the iframe fits with no scroll.
window.addEventListener('message', (e) => {
  const data = e.data;
  if (!data || data.type !== 'arch-embed-height') return;
  document.querySelectorAll('iframe.inline-view-frame').forEach((f) => {
    if (f.contentWindow === e.source) f.style.height = Math.min(760, Math.max(220, (data.height || 0) + 6)) + 'px';
  });
});

async function askStream(text) {
  add('user', text, 'you');
  history.push({role:'user', content:text});
  const pending = add('assistant', '', 'assistant');
  const bubble = pending.querySelector('.bubble');
  bubble.classList.add('loading');
  bubble.textContent = 'Working...';
  setBusy(true);

  const trace = [];
  const subagentSteps = [];
  let answer = '';
  let started = false;

  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', ...authHeaders()},
      body: JSON.stringify({question: text, session_id: currentSessionId})
    });

    if (!response.ok || !response.body) {
      let message = `Request failed (${response.status})`;
      try {
        const data = await response.json();
        message = data.error || message;
      } catch (error) {}
      throw new Error(message);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let newlineIndex;
      while ((newlineIndex = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newlineIndex);
        buffer = buffer.slice(newlineIndex + 1);
        if (!line.trim()) continue;

        const event = JSON.parse(line);
        if (event.type === 'tool_start') {
          trace.push(event.record || {tool: event.name || 'tool', seq: trace.length + 1,
                                      status: 'running'});
          showLiveTools(pending, trace);
        } else if (event.type === 'tool_end') {
          // The finished entry REPLACES the running one it matches on `seq`. Appending instead
          // would double every chip, and leaving the running one in place would leave a step that
          // has already failed still showing as in progress.
          const record = event.record;
          if (record) {
            const at = trace.findIndex((item) => item.seq === record.seq);
            if (at >= 0) trace[at] = record; else trace.push(record);
            showLiveTools(pending, trace);
          }
        } else if (event.type === 'subagent_step') {
          subagentSteps.push(event);
          showSubagent(pending, subagentSteps, event.step === 'summary');
        } else if (event.type === 'view') {
          renderInlineView(pending, event.view);
        } else if (event.type === 'token') {
          if (!started) {
            bubble.classList.remove('loading');
            bubble.textContent = '';
            started = true;
          }
          answer += event.text || '';
          bubble.textContent = answer;
          log.scrollTop = log.scrollHeight;
        } else if (event.type === 'done') {
          answer = event.answer || answer;
          bubble.classList.remove('loading');
          setBubbleContent(bubble, answer || '(empty)', true);
          markCitations(bubble, event.citations);
          history.push({role:'assistant', content: answer || ''});
          if (event.tool_trace && event.tool_trace.length) showLiveTools(pending, event.tool_trace, true);
          if (subagentSteps.length) showSubagent(pending, subagentSteps, true);
          if (event.usage) renderUsage(pending, event.usage);
          if (event.session) {
            currentSessionId = event.session.id;
            await refreshSessions(currentSessionId);
            renderFeedback(pending, event.session.id, (event.session.message_count || 0) - 1, null);
          }
        } else if (event.type === 'error') {
          bubble.classList.remove('loading');
          setBubbleContent(bubble, 'Error: ' + describeLlmError(event), false);
          if (event.reconnect_required) await refreshLlmStatus();
        }
      }
    }
  } catch (error) {
    bubble.classList.remove('loading');
    setBubbleContent(bubble, 'Request failed: ' + error.message, false);
  } finally {
    setBusy(false);
    q.focus();
  }
}

// Cards and the compact chips below them share one contract: fill the composer, do not send. The
// incident starters carry a repo id and a timestamp the demo box may want edited first.
document.querySelectorAll('[data-prompt]').forEach((button) => {
  button.addEventListener('click', () => {
    q.value = button.dataset.prompt || '';
    q.focus();
  });
});

document.addEventListener('click', (event) => {
  const pill = event.target.closest('code.cite');
  if (pill) openSource(pill.textContent || '');
});

usageButton.addEventListener('click', openUsageDashboard);

const llmButton = document.getElementById('llm-button'),
      llmPanel = document.getElementById('llm-panel'),
      llmModeTokenOption = document.getElementById('llm-mode-token-option'),
      llmModeTunnelRadio = document.getElementById('llm-mode-tunnel'),
      llmModeTokenRadio = document.getElementById('llm-mode-token'),
      llmFieldsTunnel = document.getElementById('llm-fields-tunnel'),
      llmFieldsToken = document.getElementById('llm-fields-token'),
      llmTokenEntry = document.getElementById('llm-token-entry'),
      llmTokenConnected = document.getElementById('llm-token-connected'),
      llmTokenModelSelect = document.getElementById('llm-token-model'),
      llmTunnelModelSelect = document.getElementById('llm-tunnel-model-select'),
      llmLoadModelsButton = document.getElementById('llm-load-models');

// Internal beta (SDLC_LLM_TOKEN_MODE): only known once /api/llm/me answers. Until then the token
// radio stays hidden and the panel behaves exactly like the tunnel-only original.
let tokenModeAvailable = false;
let lastLlmMode = 'shared';
let lastLlmModel = '';
let lastLlmReconnectRequired = false;

// Dynamic model selector: refreshLlmStatus can be in flight more than once (panel reopen, a
// connect/select-model call awaiting its own follow-up refresh, a slow network) -- only the LATEST
// call is allowed to touch the DOM, so a slow/out-of-order response can never clobber a newer model
// with stale data ("model pill" must never regress).
let llmStatusRevision = 0;

function applyLlmModeVisibility() {
  llmModeTokenOption.hidden = !tokenModeAvailable;
  const wantsToken = tokenModeAvailable && llmModeTokenRadio.checked;
  llmFieldsTunnel.hidden = wantsToken;
  llmFieldsToken.hidden = !wantsToken;
}

function populateModelSelect(select, models, selected) {
  // Keep the confirmed model visible even if the current listing doesn't include it (e.g. a stale
  // list, or the endpoint briefly reporting fewer models) -- synthesize an <option> for it rather
  // than just tracking an id that never becomes a selectable option.
  const normalized = [...(models || [])];
  if (selected && !normalized.some(m => m.id === selected)) {
    normalized.unshift({id: selected, label: selected});
  }
  select.innerHTML = '';
  for (const model of normalized) {
    const opt = document.createElement('option');
    opt.value = model.id;
    opt.textContent = model.label || model.id;
    select.appendChild(opt);
  }
  if (selected) select.value = selected;
}

async function loadTokenModelOptions(selected, revision) {
  try {
    const listing = await fetchJson('/api/llm/models');
    // Check BEFORE touching the DOM: a slower, now-superseded call must never mutate the dropdown
    // just because it happens to resolve after a newer refreshLlmStatus() already started.
    if (revision !== llmStatusRevision) return;
    populateModelSelect(llmTokenModelSelect, listing.models, selected || listing.default_model);
  } catch (error) {
    // /models may be the first request to observe a revoked/expired credential. The server has
    // already owner-disconnected it; fetch its authoritative state so the pill cannot remain
    // misleadingly "Connected".
    if (error.reconnectRequired || error.status === 401 || error.status === 403) {
      await refreshLlmStatus();
    }
  }
}

async function refreshLlmStatus(tokenListing = null) {
  const myRevision = ++llmStatusRevision;
  try {
    const me = await fetchJson('/api/llm/me');
    if (myRevision !== llmStatusRevision) return;  // a newer refresh already landed -- drop this one
    tokenModeAvailable = !!(me && me.token_mode_available);
    lastLlmMode = (me && me.mode) || (me && me.registered ? 'tunnel' : 'shared');
    lastLlmModel = (me && me.model) || '';
    lastLlmReconnectRequired = !!(me && me.reconnect_required);
    // Sync the radio choice to what the server actually reports -- otherwise a page reload after
    // connecting in Token mode shows the confirmed state in the pill/panel fields while the radio
    // (and therefore which fields are visible) silently defaults back to Tunnel.
    if (lastLlmMode === 'copilot_token') llmModeTokenRadio.checked = true;
    if (lastLlmMode === 'tunnel') llmModeTunnelRadio.checked = true;
    applyLlmModeVisibility();

    const isTokenMode = lastLlmMode === 'copilot_token';
    // A credential-shaped token that no longer resolves reports mode=copilot_token WITHOUT
    // registered=true (reconnect_required instead) -- that must show the paste-token entry again,
    // not the "connected" summary view.
    const tokenConnected = isTokenMode && !!(me && me.registered);
    llmTokenEntry.hidden = tokenConnected;
    llmTokenConnected.hidden = !tokenConnected;
    if (tokenConnected) {
      if (tokenListing) {
        // POST /connect-token already returned this authenticated listing. Reuse it rather than
        // racing a second request against the just-created credential.
        populateModelSelect(llmTokenModelSelect, tokenListing.models,
          lastLlmModel || tokenListing.selected_model || tokenListing.model || tokenListing.default_model);
      } else {
        await loadTokenModelOptions(lastLlmModel, myRevision);
      }
    }
    if (myRevision !== llmStatusRevision) return;  // re-check: the model-list fetch above awaited

    if (me && me.registered) {
      const label = isTokenMode ? (me.label || 'Copilot token') : (me.label || 'mine');
      llmButton.textContent = 'LLM: ' + label + (lastLlmModel ? ' · ' + lastLlmModel : '');
      llmButton.title = (isTokenMode
        ? 'Your requests use your own Copilot token (no tunnel)'
        : 'Your requests route to ' + (me.base_url || 'your endpoint'))
        + (lastLlmModel ? ' — model: ' + lastLlmModel : '');
    } else if (lastLlmReconnectRequired) {
      llmButton.textContent = 'LLM: token reconnect required';
      llmButton.title = 'The in-memory Copilot credential is gone. Reconnect or click Disconnect.';
      document.getElementById('llm-status').textContent =
        'Token connection expired or the server restarted. Paste the token again, ' +
        'or click Disconnect to use the shared LLM.';
    } else {
      llmButton.textContent = 'LLM: shared' + (lastLlmModel ? ' · ' + lastLlmModel : '');
      llmButton.title = 'Using the server default'
        + (lastLlmModel ? ' (model: ' + lastLlmModel + ')' : '')
        + ' — click to connect your own LLM';
    }
  } catch (error) { /* leave the pill as-is */ }
}

async function connectLlmTunnel(statusEl) {
  const base_url = document.getElementById('llm-base').value.trim();
  const label = document.getElementById('llm-label').value.trim();
  const model = document.getElementById('llm-model').value.trim();
  if (!base_url) { statusEl.textContent = 'Enter your endpoint first.'; return; }
  statusEl.textContent = 'Connecting (checking the endpoint + model)...';
  // Reuse the existing route token only when ALREADY in tunnel mode (updating our own
  // registration); when switching FROM Token mode, `userToken` holds a credential_id, which must
  // never become a route token -- send it as previous_credential_id instead, so the server retires
  // it (only after the new tunnel registration actually succeeds).
  const tunnelToken = lastLlmMode === 'tunnel' ? (userToken || undefined) : undefined;
  const previousCredentialId = lastLlmMode === 'copilot_token' ? (userToken || undefined) : undefined;
  const record = await fetchJson('/api/llm/register', {
    method: 'POST',
    body: JSON.stringify({
      base_url, label, model,
      token: tunnelToken,
      previous_credential_id: previousCredentialId,
    })
  });
  userToken = record.token;
  localStorage.setItem('sdlc_user_token', userToken);
  statusEl.textContent = 'Connected — your chats now use ' + record.base_url
    + (record.model ? ' (' + record.model + ')' : '');
}

async function connectLlmToken(statusEl) {
  const tokenInput = document.getElementById('llm-token');
  const token = tokenInput.value.trim();
  // Blank token: stay in Token mode and show the error -- never silently fall back to Tunnel mode.
  if (!token) { statusEl.textContent = 'Paste your .copilot_token first.'; return; }
  statusEl.textContent = 'Connecting (verifying the token + a model)...';
  const record = await fetchJson('/api/llm/connect-token', {
    method: 'POST',
    body: JSON.stringify({token})
  });
  userToken = record.credential_id;
  localStorage.setItem('sdlc_user_token', userToken);
  tokenInput.value = '';  // never leave the pasted token sitting in the DOM
  statusEl.textContent = 'Connected — your chats now use your own Copilot token'
    + (record.model ? ' (' + record.model + ')' : '') + '.';
  return record;
}

function describeLlmError(error) {
  // fetchJson() exposes camelCase metadata, while NDJSON chat-stream events are the server's JSON
  // payload verbatim. Accept both shapes so a structured 429 always becomes an actionable hint.
  if (error && (error.status === 429 || error.code === 'copilot_rate_limit')) {
    const wait = Number(error.retryAfter ?? error.retry_after);
    const waitHint = Number.isFinite(wait) && wait > 0 ? ` Try again in ${wait} seconds.` : '';
    return 'Copilot is rate limited.' + waitHint
      + (error.retryable ? ' You can retry later or choose another model.' : '');
  }
  return (error && (error.message || error.error)) || 'Request failed.';
}

async function refreshLlmStatusAfterAuthFailure(error) {
  if (error && (error.reconnectRequired || error.status === 401 || error.status === 403)) {
    await refreshLlmStatus();
    return true;
  }
  return false;
}

async function connectLlm() {
  const statusEl = document.getElementById('llm-status');
  try {
    let tokenListing = null;
    if (tokenModeAvailable && llmModeTokenRadio.checked) {
      tokenListing = await connectLlmToken(statusEl);
    } else {
      await connectLlmTunnel(statusEl);
    }
    await refreshLlmStatus(tokenListing);
  } catch (error) {
    statusEl.textContent = 'Failed: ' + describeLlmError(error);
    await refreshLlmStatusAfterAuthFailure(error);
  }
}

async function disconnectLlm() {
  const statusEl = document.getElementById('llm-status');
  try {
    if (lastLlmMode === 'copilot_token' && userToken) {
      await fetchJson('/api/llm/disconnect-token', {
        method: 'POST',
        body: JSON.stringify({credential_id: userToken})
      });
    }
    userToken = '';
    localStorage.removeItem('sdlc_user_token');
    statusEl.textContent = 'Disconnected — your chats now use the shared default LLM.';
    await refreshLlmStatus();
  } catch (error) {
    statusEl.textContent = 'Failed: ' + error.message;
  }
}

async function switchModel(selectEl, statusEl) {
  const previous = lastLlmModel;
  const next = selectEl.value;
  if (!next || next === previous) return;
  statusEl.textContent = 'Switching model (re-checking the connection)...';
  try {
    const result = await fetchJson('/api/llm/select-model', {
      method: 'POST',
      body: JSON.stringify({model: next})
    });
    statusEl.textContent = 'Model switched to ' + result.model + '.';
    await refreshLlmStatus();
  } catch (error) {
    selectEl.value = previous;  // failed switch keeps the previously-confirmed model active
    statusEl.textContent = 'Failed: ' + describeLlmError(error)
      + ' — kept using ' + (previous || 'the previous model') + '.';
    await refreshLlmStatusAfterAuthFailure(error);
  }
}

llmModeTunnelRadio.addEventListener('change', applyLlmModeVisibility);
llmModeTokenRadio.addEventListener('change', applyLlmModeVisibility);

llmTokenModelSelect.addEventListener('change', () => {
  switchModel(llmTokenModelSelect, document.getElementById('llm-status'));
});

llmLoadModelsButton.addEventListener('click', async () => {
  const statusEl = document.getElementById('llm-status');
  const base_url = document.getElementById('llm-base').value.trim();
  if (!base_url) { statusEl.textContent = 'Enter your endpoint first.'; return; }
  statusEl.textContent = 'Loading models...';
  try {
    const listing = await fetchJson('/api/llm/tunnel-models', {
      method: 'POST',
      body: JSON.stringify({base_url})
    });
    populateModelSelect(llmTunnelModelSelect, listing.models, document.getElementById('llm-model').value.trim());
    llmTunnelModelSelect.hidden = false;
    statusEl.textContent = listing.models.length
      ? 'Loaded ' + listing.models.length + ' model(s) — pick one, or keep typing a model id manually.'
      : 'Endpoint reachable but reported no models — type a model id manually.';
  } catch (error) {
    statusEl.textContent = 'Failed to load models: ' + error.message;
  }
});

llmTunnelModelSelect.addEventListener('change', () => {
  // The manual field stays the single source of truth for Connect -- picking from the dropdown
  // just fills it in, so a typed model id keeps working exactly as before.
  document.getElementById('llm-model').value = llmTunnelModelSelect.value;
});

llmButton.addEventListener('click', () => {
  usagePanel.hidden = true;
  mcpPanel.hidden = true;
  llmPanel.hidden = !llmPanel.hidden;
  if (!llmPanel.hidden) {
    const base = document.getElementById('llm-base');
    if (!base.value) base.value = 'http://127.0.0.1:4141/v1';
    applyLlmModeVisibility();
    (llmFieldsToken.hidden ? base : document.getElementById('llm-token')).focus();
  }
});
document.getElementById('llm-connect').addEventListener('click', connectLlm);
document.getElementById('llm-disconnect').addEventListener('click', disconnectLlm);

sessionListEl.addEventListener('click', async (event) => {
  const item = event.target.closest('.session-item');
  if (!item || item.dataset.sessionId === currentSessionId) return;
  setBusy(true);
  try {
    await loadSession(item.dataset.sessionId);
  } catch (error) {
    setSessionMeta(`Failed to load session: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

newSessionButton.addEventListener('click', async () => {
  setBusy(true);
  try {
    await createSession();
    q.focus();
  } catch (error) {
    setSessionMeta(`Failed to create session: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

f.addEventListener('submit', e => {
  e.preventDefault();
  const t = q.value.trim();
  if (!t) return;
  q.value = '';
  askStream(t);
});

q.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    f.requestSubmit();
  }
});

resetLog(true);
initializeSessions();
refreshIndexStatus();
refreshMcpStatus();
refreshLlmStatus();

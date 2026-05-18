"""Local web UI for docsearch.

Architecture:
  GET /          → renders shell + indexed results (instant FTS5 query).
  GET /stream    → SSE: walks unindexed files, extracts in a thread pool,
                   streams matches as `result` events, persists to the index.
  GET /dirs      → JSON listing of one directory level (lazy folder picker).
  GET /open      → opens a hit file in the system viewer.
"""
import hmac
import html
import http.cookies
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import index
from .config import load_config

MAX_EXTRACTORS = 4
SESSION_COOKIE = "docsearch_session"
AUTH_TOKEN_FILENAME = "auth-token"

# Hard caps for the folder-picker so a pathological directory can never DoS us.
MAX_DIR_CHILDREN = 5000     # subfolders shown per /dirs call
MAX_FOLDER_SELECTIONS = 200  # selected folders accepted per search


def _normpath(p: str) -> str:
    """Expanduser + normpath. Lexical only — does NOT follow symlinks. We
    rely on this both for the auth check (a `..`-laden path normalizes to a
    form that no longer starts with an authorized root) and for prefix
    matching against indexed paths (which are stored without resolution)."""
    return os.path.normpath(os.path.expanduser(p))


def _path_under(child: str, parent: str) -> bool:
    """True iff `child` equals `parent` or is a descendant. Both must already
    be normalized. Uses os.sep boundary so /foo doesn't match /foobar."""
    return child == parent or child.startswith(parent + os.sep)


def _resolve_within_roots(path_str: str, roots: list[str]) -> Path | None:
    """Return the normalized Path iff it points at an existing directory that
    lies inside one of the configured `roots`. Otherwise None. Purely
    lexical — does not follow symlinks, so loops/escapes via symlink can't
    bypass the check."""
    if not path_str:
        return None
    norm = _normpath(path_str)
    for r in roots:
        if _path_under(norm, _normpath(r)):
            p = Path(norm)
            # is_dir() does follow symlinks, but that's OK here: we only use
            # this to display children, never to recurse, and the lexical
            # prefix check above already prevented escape.
            if p.is_dir():
                return p
            return None
    return None


def _dedupe_descendants(paths: Iterable[str]) -> list[str]:
    """Drop any path whose ancestor is already in the set. Parent wins.
    Inputs must already be normalized. Output is sorted lexicographically
    and capped at MAX_FOLDER_SELECTIONS."""
    unique = sorted(set(paths))
    out: list[str] = []
    for p in unique:
        if any(_path_under(p, o) for o in out):
            continue
        out.append(p)
        if len(out) >= MAX_FOLDER_SELECTIONS:
            break
    return out


def _parse_folders_param(raw: str, configured: list[str]) -> tuple[list[str], bool]:
    """Parse the `folders` query param (newline-separated absolute paths).
    Returns (effective_folders, is_filtered):
      - is_filtered=False → no valid selection given; use all configured folders.
      - is_filtered=True  → restrict to the returned list.
    Selections outside the configured roots are silently dropped (defensive —
    /dirs can't produce them, but a hand-crafted URL might)."""
    if not raw:
        return list(configured), False
    requested = [s for s in (line.strip() for line in raw.replace(",", "\n").splitlines()) if s]
    valid: list[str] = []
    for r in requested:
        norm = _normpath(r)
        if any(_path_under(norm, _normpath(c)) for c in configured):
            valid.append(norm)
    if not valid:
        return list(configured), False
    return _dedupe_descendants(valid), True


def _has_dir_child(path: Path) -> bool:
    """Cheap probe: does `path` contain at least one non-hidden subdirectory?
    Uses follow_symlinks=False to avoid traversing into symlink loops."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --link: #3366cc;
    --link-visited: #6b4ba1;
    --text: #202122;
    --muted: #54595d;
    --rule: #a2a9b1;
    --bg: #ffffff;
    --hit-bg: #fff3b0;
  }}
  body {{ font-family: "Linux Libertine", Georgia, "Times New Roman", serif;
         color: var(--text); background: var(--bg);
         max-width: 880px; margin: 0 auto; padding: 28px 32px 80px; }}
  header {{ border-bottom: 1px solid var(--rule); padding-bottom: 14px; margin-bottom: 22px; }}
  h1 {{ font-size: 1.8em; font-weight: normal; margin: 0 0 8px; }}
  h1 a {{ color: inherit; text-decoration: none; }}
  form {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  input[type=text] {{ flex: 1 1 320px; padding: 8px 10px; font-size: 1em;
                      border: 1px solid var(--rule); border-radius: 2px;
                      font-family: inherit; }}
  select, input[type=number] {{ padding: 7px 8px; font-size: 0.95em;
                                border: 1px solid var(--rule); border-radius: 2px;
                                font-family: inherit; background: white; }}
  button {{ padding: 8px 16px; font-size: 1em; cursor: pointer;
            background: #f8f9fa; border: 1px solid var(--rule); border-radius: 2px;
            font-family: inherit; }}
  button:hover {{ background: #eaecf0; }}
  .meta {{ color: var(--muted); font-size: 0.9em; margin: 14px 0 18px;
           font-family: -apple-system, sans-serif; }}
  .indexing {{ color: var(--muted); font-size: 0.9em; margin: 8px 0 18px;
               font-family: -apple-system, sans-serif; }}
  .indexing .bar {{ display: inline-block; vertical-align: middle;
                    width: 160px; height: 6px; background: #eaecf0;
                    border-radius: 3px; margin-left: 8px; overflow: hidden; }}
  .indexing .bar > span {{ display: block; height: 100%; background: var(--link);
                           width: 0%; transition: width 0.2s; }}
  .result {{ border-bottom: 1px solid #eaecf0; padding: 16px 0; }}
  .result h2 {{ font-size: 1.15em; margin: 0 0 4px; font-weight: normal; }}
  .result h2 a {{ color: var(--link); text-decoration: none; }}
  .result h2 a:hover {{ text-decoration: underline; }}
  .result h2 a:visited {{ color: var(--link-visited); }}
  .path {{ color: #006622; font-size: 0.85em; font-family: -apple-system, sans-serif;
           margin-bottom: 6px; word-break: break-all; }}
  .count {{ color: var(--muted); font-size: 0.9em; font-family: -apple-system, sans-serif; }}
  .snippets {{ margin-top: 6px; }}
  .snippet {{ margin: 4px 0; padding-left: 14px; border-left: 2px solid #eaecf0;
              font-size: 0.95em; line-height: 1.45; }}
  .loc {{ color: var(--muted); font-family: -apple-system, sans-serif;
          font-size: 0.82em; margin-right: 6px; }}
  mark {{ background: var(--hit-bg); padding: 0 2px; }}
  .more {{ color: var(--muted); font-size: 0.85em; padding-left: 14px;
           font-family: -apple-system, sans-serif; }}
  .empty {{ color: var(--muted); padding: 40px 0; text-align: center; font-size: 1.05em; }}
  footer {{ margin-top: 48px; padding-top: 12px; border-top: 1px solid var(--rule);
            color: var(--muted); font-size: 0.8em; font-family: -apple-system, sans-serif; }}
  footer code {{ font-size: 0.95em; }}
  .folder-summary {{ color: var(--muted); font-size: 0.85em; margin-top: 8px;
                     font-family: -apple-system, sans-serif; }}
  .folder-summary a {{ color: var(--link); }}
  .folder-panel {{ margin-top: 12px; padding: 12px; border: 1px solid var(--rule);
                   border-radius: 2px; background: #fbfbfd;
                   font-family: -apple-system, sans-serif; font-size: 0.92em; }}
  .folder-panel[hidden] {{ display: none; }}
  .folder-panel .actions {{ margin-top: 10px; display: flex; gap: 8px; }}
  .folder-tree {{ list-style: none; padding: 0; margin: 0; max-height: 340px;
                  overflow-y: auto; }}
  .folder-tree ul {{ list-style: none; padding-left: 18px; margin: 0; }}
  .tree-row {{ display: flex; align-items: center; gap: 6px; padding: 2px 0;
               line-height: 1.5; }}
  .tree-toggle {{ display: inline-block; width: 14px; text-align: center;
                  color: var(--muted); user-select: none; }}
  .tree-label {{ word-break: break-all; }}
  .tree-empty {{ color: var(--muted); font-style: italic; padding: 2px 0 2px 20px; }}
  .tree-error {{ color: #b32424; padding: 2px 0 2px 20px; }}
  .tree-truncated {{ color: var(--muted); padding: 2px 0 2px 20px; }}
</style>
</head>
<body>
<header>
  <h1><a href="/">docsearch</a></h1>
  <form method="get" action="/" id="search-form">
    <input type="text" name="q" value="{q_attr}" placeholder="Search your documents…" autofocus>
    <select name="mode" aria-label="Match mode">
      <option value="all"{mode_all_sel}>all words</option>
      <option value="phrase"{mode_phrase_sel}>exact phrase</option>
      <option value="any"{mode_any_sel}>any word</option>
    </select>
    <select name="types" aria-label="File type">
      {types_options}
    </select>
    <button type="button" id="folders-btn" aria-expanded="false">Folders…</button>
    <input type="hidden" name="folders" id="folders-input" value="{folders_attr}">
    <button type="submit">Search</button>
  </form>
  <div class="folder-summary" id="folder-summary">{folders_summary}</div>
  <div class="folder-panel" id="folder-panel" hidden>
    <ul class="folder-tree" id="folder-tree"><li class="tree-empty">Loading…</li></ul>
    <div class="actions">
      <button type="button" id="folders-apply">Apply</button>
      <button type="button" id="folders-clear">Clear (search all)</button>
      <button type="button" id="folders-close">Close</button>
    </div>
  </div>
</header>
{body}
{folder_picker_script}
{stream_script}
<footer>
  Index: <code>{db_path_attr}</code> &mdash; persists across restarts.
  New or changed files are indexed automatically on search.
  PDFs require <code>brew install poppler</code>.
  &nbsp;&middot;&nbsp; version <code>{app_version_attr}</code>
</footer>
</body>
</html>
"""

FOLDER_PICKER_SCRIPT = """
<script>
(function() {
  const btn = document.getElementById('folders-btn');
  const panel = document.getElementById('folder-panel');
  const tree = document.getElementById('folder-tree');
  const input = document.getElementById('folders-input');
  const form = document.getElementById('search-form');
  const applyBtn = document.getElementById('folders-apply');
  const clearBtn = document.getElementById('folders-clear');
  const closeBtn = document.getElementById('folders-close');
  const resetLink = document.getElementById('folders-reset');
  if (!btn || !panel || !tree || !input || !form) return;

  let loaded = false;
  // Bound concurrent /dirs requests so a frustrated user can't open
  // hundreds of nodes at once. Each row also disables its toggle while
  // its request is in flight to prevent re-entry.
  let inFlight = 0;
  const MAX_INFLIGHT = 8;

  function setPanelOpen(open) {
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !loaded) {
      loaded = true;
      loadInto(tree, '');
    }
  }

  function makeNode(child) {
    const li = document.createElement('li');
    li.dataset.path = child.path;
    const row = document.createElement('div');
    row.className = 'tree-row';

    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle';
    if (child.has_children) {
      toggle.textContent = '\\u25B8';
      toggle.style.cursor = 'pointer';
      toggle.addEventListener('click', () => toggleNode(li, toggle));
    } else {
      toggle.textContent = '\\u00A0';
    }

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'tree-check';

    const label = document.createElement('span');
    label.className = 'tree-label';
    label.textContent = child.name;
    if (child.has_children) {
      label.style.cursor = 'pointer';
      label.addEventListener('click', () => toggleNode(li, toggle));
    }

    row.appendChild(toggle);
    row.appendChild(checkbox);
    row.appendChild(label);
    li.appendChild(row);
    return li;
  }

  function loadInto(container, path) {
    if (inFlight >= MAX_INFLIGHT) return;
    inFlight++;
    fetch('/dirs?path=' + encodeURIComponent(path), {credentials: 'same-origin'})
      .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
      .then(data => {
        container.innerHTML = '';
        if (!data.children || data.children.length === 0) {
          const li = document.createElement('li');
          li.className = 'tree-empty';
          li.textContent = '(no subfolders)';
          container.appendChild(li);
          return;
        }
        for (const c of data.children) container.appendChild(makeNode(c));
        if (data.truncated) {
          const li = document.createElement('li');
          li.className = 'tree-truncated';
          li.textContent = '(too many subfolders to show — refine first)';
          container.appendChild(li);
        }
      })
      .catch(err => {
        container.innerHTML = '';
        const li = document.createElement('li');
        li.className = 'tree-error';
        li.textContent = 'failed to load: ' + err.message;
        container.appendChild(li);
      })
      .finally(() => { inFlight--; });
  }

  function toggleNode(li, toggle) {
    const existing = li.querySelector(':scope > ul');
    if (existing) {
      existing.remove();
      toggle.textContent = '\\u25B8';
      return;
    }
    if (toggle.dataset.loading === '1') return;
    toggle.dataset.loading = '1';
    toggle.textContent = '\\u25BE';
    const ul = document.createElement('ul');
    const placeholder = document.createElement('li');
    placeholder.className = 'tree-empty';
    placeholder.textContent = 'Loading…';
    ul.appendChild(placeholder);
    li.appendChild(ul);
    // Inline loader so we can re-target this exact ul.
    if (inFlight >= MAX_INFLIGHT) {
      ul.innerHTML = '<li class="tree-error">too many open requests — wait a moment</li>';
      toggle.dataset.loading = '';
      toggle.textContent = '\\u25B8';
      ul.remove();
      return;
    }
    inFlight++;
    fetch('/dirs?path=' + encodeURIComponent(li.dataset.path), {credentials: 'same-origin'})
      .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
      .then(data => {
        ul.innerHTML = '';
        if (!data.children || data.children.length === 0) {
          ul.innerHTML = '<li class="tree-empty">(no subfolders)</li>';
          return;
        }
        for (const c of data.children) ul.appendChild(makeNode(c));
        if (data.truncated) {
          const trunc = document.createElement('li');
          trunc.className = 'tree-truncated';
          trunc.textContent = '(too many subfolders to show — refine first)';
          ul.appendChild(trunc);
        }
      })
      .catch(err => {
        ul.innerHTML = '<li class="tree-error">failed to load: ' + err.message + '</li>';
        toggle.textContent = '\\u25B8';
      })
      .finally(() => {
        inFlight--;
        toggle.dataset.loading = '';
      });
  }

  function collectChecked() {
    const checks = tree.querySelectorAll('input.tree-check:checked');
    const paths = [];
    checks.forEach(c => {
      const li = c.closest('li');
      if (li && li.dataset.path) paths.push(li.dataset.path);
    });
    // parent-wins dedupe so a redundant child selection collapses up.
    paths.sort();
    const kept = [];
    const sep = '/';
    for (const p of paths) {
      if (kept.some(k => p === k || p.startsWith(k + sep))) continue;
      kept.push(p);
    }
    return kept;
  }

  btn.addEventListener('click', () => setPanelOpen(panel.hidden));
  closeBtn.addEventListener('click', () => setPanelOpen(false));
  applyBtn.addEventListener('click', () => {
    const kept = collectChecked();
    input.value = kept.join('\\n');
    form.submit();
  });
  clearBtn.addEventListener('click', () => {
    input.value = '';
    form.submit();
  });
  if (resetLink) {
    resetLink.addEventListener('click', (e) => {
      e.preventDefault();
      input.value = '';
      form.submit();
    });
  }
})();
</script>
"""

STREAM_SCRIPT_TPL = """
<script>
(function() {{
  const q = {q_json};
  const types = {types_json};
  const folders = {folders_json};
  const mode = {mode_json};
  const limit = {limit};
  const params = new URLSearchParams({{q: q, types: types, mode: mode, limit: String(limit)}});
  if (folders) params.set('folders', folders);
  const es = new EventSource('/stream?' + params.toString());
  const indexingEl = document.getElementById('indexing');
  const resultsEl = document.getElementById('results');
  const countEl = document.getElementById('result-count');
  let docCount = parseInt(countEl.dataset.docs || '0', 10);
  let hitCount = parseInt(countEl.dataset.hits || '0', 10);
  let total = 0, done = 0;
  es.addEventListener('progress', (e) => {{
    const d = JSON.parse(e.data);
    if (d.total !== undefined) total = d.total;
    if (d.done !== undefined) done = d.done;
    if (total > 0) {{
      const pct = Math.round(100 * done / total);
      indexingEl.style.display = '';
      indexingEl.innerHTML = `Indexing ${{done}} of ${{total}} new files… <span class="bar"><span style="width:${{pct}}%"></span></span>`;
    }}
  }});
  es.addEventListener('result', (e) => {{
    const r = JSON.parse(e.data);
    docCount += 1;
    hitCount += r.snippets.length;
    countEl.dataset.docs = docCount;
    countEl.dataset.hits = hitCount;
    countEl.textContent = docCount + ' document(s), ' + hitCount + ' match(es)';
    resultsEl.insertAdjacentHTML('beforeend', r.html);
  }});
  es.addEventListener('done', (e) => {{
    indexingEl.style.display = 'none';
    es.close();
  }});
  es.onerror = () => {{ es.close(); indexingEl.style.display = 'none'; }};
}})();
</script>
"""


# --- snippet rendering ------------------------------------------------------

def _highlight(text: str, query: str) -> str:
    escaped = html.escape(text)
    is_phrase, raw_terms = index._parse_match_expr(query)
    terms = [t for t in raw_terms if t]
    if not terms:
        return escaped
    folded, idx_map = index._fold(escaped)
    folded_terms = [index._fold(t)[0] for t in terms]
    folded_terms = [t for t in folded_terms if t]
    if not folded_terms:
        return escaped
    if is_phrase:
        pattern = re.compile(
            r"\b" + r"\s+".join(re.escape(t) for t in folded_terms) + r"\b"
        )
    else:
        uniq = sorted(set(folded_terms), key=len, reverse=True)
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(t) for t in uniq) + r")\b"
        )
    out: list[str] = []
    cursor = 0
    elen = len(escaped)
    for m in pattern.finditer(folded):
        s = index._idx(idx_map, m.start(), elen)
        e = index._idx(idx_map, m.end() - 1, elen - 1) + 1
        out.append(escaped[cursor:s])
        out.append("<mark>")
        out.append(escaped[s:e])
        out.append("</mark>")
        cursor = e
    out.append(escaped[cursor:])
    return "".join(out)


def _render_result_html(path: Path, snippets: list[dict], expr: str, limit: int) -> str:
    open_url = "/open?path=" + urllib.parse.quote(str(path))
    n = len(snippets)
    parts = ['<div class="result">']
    parts.append(f'<h2><a href="{open_url}">{html.escape(path.name)}</a></h2>')
    parts.append(f'<div class="path">{html.escape(str(path))}</div>')
    parts.append(f'<div class="count">{n} match{"es" if n != 1 else ""}</div>')
    shown = snippets if limit == 0 else snippets[:limit]
    parts.append('<div class="snippets">')
    for loc in shown:
        if "page" in loc:
            tag = f"p.{loc['page']}"
        else:
            tag = f"L{loc.get('line', '?')}"
        snippet_html = _highlight(loc["text"], expr)
        parts.append(
            f'<div class="snippet"><span class="loc">{tag}</span>'
            f"…{snippet_html}…</div>"
        )
    parts.append("</div>")
    if limit and n > limit:
        parts.append(f'<div class="more">… {n - limit} more matches in this file</div>')
    parts.append("</div>")
    return "".join(parts)


def _render_inline(results: list, expr: str, limit: int) -> tuple[str, int, int]:
    total_hits = sum(len(s) for _, s in results)
    parts: list[str] = []
    for path, snippets in results:
        parts.append(_render_result_html(path, snippets, expr, limit))
    return "".join(parts), len(results), total_hits


# --- HTTP handler -----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    cfg: dict | None = None
    db_path: Path | None = None
    session_token: str | None = None
    bound_port: int | None = None
    app_version: str = "dev"

    def log_message(self, fmt, *args):
        sys.stderr.write("[docsearch] " + (fmt % args) + "\n")

    # --- auth ------------------------------------------------------------

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _origin_allowed(self) -> bool:
        """Reject only if a request *carries* an Origin that isn't ours.
        Direct tools like curl typically omit the header — those still have
        to pass the cookie/token check."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        allowed = f"http://127.0.0.1:{self.bound_port}"
        return origin == allowed

    def _authenticate(self, parsed) -> str:
        """Return one of: 'ok', 'set-cookie', 'reject', 'cross-origin'.

        - 'cross-origin' → 403 (a browser made the call from a different page).
        - 'set-cookie'   → 302 redirect that drops the token from the URL and
                           stores a session cookie. Only happens on the
                           first navigation from the launch URL.
        - 'ok'           → the cookie is valid; serve the request.
        - 'reject'       → 401 with a hint to use the launch URL.
        """
        if not self._origin_allowed():
            return "cross-origin"
        token = self.session_token
        if not token:
            return "reject"  # server isn't fully initialized; deny.
        qs = urllib.parse.parse_qs(parsed.query)
        supplied = qs.get("token", [None])[0]
        if supplied is not None and hmac.compare_digest(supplied, token):
            return "set-cookie"
        cookie = self._cookie_token()
        if cookie and hmac.compare_digest(cookie, token):
            return "ok"
        return "reject"

    def _set_cookie_and_redirect(self, parsed):
        """Set the session cookie, then 302 to the same path with the token
        query parameter removed (so it doesn't sit in browser history)."""
        qs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
              if k != "token"]
        new_query = urllib.parse.urlencode(qs)
        target = parsed.path + (("?" + new_query) if new_query else "")
        cookie = (
            f"{SESSION_COOKIE}={self.session_token}; Path=/; "
            f"HttpOnly; SameSite=Strict"
        )
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _reject(self, code: int, reason: str):
        body = f"{code} {reason}\n".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Belt-and-suspenders: all assets are inline; deny any external fetch.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _open_db(self):
        return index.open_db(self.db_path or index.default_db_path())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        auth = self._authenticate(parsed)
        if auth == "cross-origin":
            self._reject(403, "forbidden: cross-origin request")
            return
        if auth == "reject":
            self._reject(
                401,
                "unauthorized: launch via the URL printed by `docsearch-web` "
                "(includes a one-time session token)",
            )
            return
        if auth == "set-cookie":
            self._set_cookie_and_redirect(parsed)
            return
        if parsed.path == "/":
            self._handle_search(parsed)
        elif parsed.path == "/stream":
            self._handle_stream(parsed)
        elif parsed.path == "/dirs":
            self._handle_dirs(parsed)
        elif parsed.path == "/open":
            self._handle_open(parsed)
        else:
            self._send(404, "not found")

    # /dirs — JSON, one directory level
    def _handle_dirs(self, parsed):
        """Return the immediate subdirectories of the requested path.

        Empty/missing `path` → the configured root folders. Otherwise the
        path must lie inside one of those roots (lexical check, no symlink
        following). Results capped at MAX_DIR_CHILDREN; hidden entries
        skipped. Symlinks to dirs are NOT listed — this is the loop-safety
        guarantee for the picker: each call descends exactly one real
        directory level and can never enter a symlink cycle."""
        qs = urllib.parse.parse_qs(parsed.query)
        requested = qs.get("path", [""])[0]

        if not requested:
            children = []
            seen: set[str] = set()
            for r in self.cfg["folders"]:
                norm = _normpath(r)
                if norm in seen:
                    continue
                seen.add(norm)
                p = Path(norm)
                if not p.is_dir():
                    continue
                children.append({
                    "name": str(p),
                    "path": norm,
                    "has_children": _has_dir_child(p),
                })
            payload = json.dumps({"path": "", "children": children})
            self._send(200, payload, ctype="application/json; charset=utf-8")
            return

        target = _resolve_within_roots(requested, self.cfg["folders"])
        if target is None:
            self._reject(403, "forbidden: path is not within a configured folder")
            return

        children = []
        truncated = False
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if len(children) >= MAX_DIR_CHILDREN:
                        truncated = True
                        break
                    if entry.name.startswith("."):
                        continue
                    try:
                        # follow_symlinks=False — a symlink to a dir is not
                        # listed, so the picker cannot enter a loop.
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    cp = Path(entry.path)
                    children.append({
                        "name": entry.name,
                        "path": _normpath(str(cp)),
                        "has_children": _has_dir_child(cp),
                    })
        except OSError as e:
            self._reject(500, f"could not list directory: {e.__class__.__name__}")
            return
        children.sort(key=lambda c: c["name"].lower())
        payload = json.dumps({
            "path": str(target),
            "children": children,
            "truncated": truncated,
        })
        self._send(200, payload, ctype="application/json; charset=utf-8")

    # /open
    def _handle_open(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        p = qs.get("path", [""])[0]
        if p and Path(p).exists():
            subprocess.Popen(["open", p])
            self._send(200, "<script>history.back()</script>opening…")
        else:
            self._send(404, "not found")

    def _render_types_options(self, selection: str) -> str:
        """Build <option> tags for the file-type dropdown.

        `selection` is either 'all' or a comma-joined list of types matching
        one of the configured single-type options."""
        opts = ['<option value="all"{sel}>all types</option>'.format(
            sel=" selected" if selection == "all" else ""
        )]
        for t in self.cfg["types"]:
            sel = " selected" if selection == t else ""
            opts.append(
                f'<option value="{html.escape(t, quote=True)}"{sel}>'
                f'{html.escape(t)} only</option>'
            )
        return "\n      ".join(opts)

    # /
    def _handle_search(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        types_str = qs.get("types", [""])[0]
        if types_str.strip().lower() in ("", "all"):
            types_str = ""
            types = list(self.cfg["types"])
            type_selection = "all"
        else:
            types = [t.strip().lstrip(".") for t in types_str.split(",") if t.strip()]
            if not types:
                types = list(self.cfg["types"])
                type_selection = "all"
            else:
                type_selection = ",".join(types)
        folders_raw = qs.get("folders", [""])[0]
        effective_folders, folders_filtered = _parse_folders_param(
            folders_raw, self.cfg["folders"]
        )
        mode = qs.get("mode", [self.cfg.get("mode") or "all"])[0]
        if mode not in ("all", "phrase", "any"):
            mode = "all"
        try:
            limit = max(0, int(qs.get("limit", ["5"])[0]))
        except ValueError:
            limit = 5

        body_parts: list[str] = []
        stream_script = ""
        expr = index.build_match_expr(q, mode)

        if q.strip():
            db = self._open_db()
            try:
                results = index.search(db, q, mode=mode)
                query_error = None
            except Exception as exc:
                results = []
                query_error = str(exc)
            finally:
                db.close()
            if query_error:
                body_parts.append(
                    f'<div class="empty">Invalid query: '
                    f'<code>{html.escape(query_error)}</code></div>'
                )
                title = "docsearch"
                page = PAGE.format(
                    title=html.escape(title),
                    q_attr=html.escape(q, quote=True),
                    types_options=self._render_types_options(type_selection),
                    mode_all_sel=' selected' if mode == "all" else "",
                    mode_phrase_sel=' selected' if mode == "phrase" else "",
                    mode_any_sel=' selected' if mode == "any" else "",
                    folders_attr=html.escape(folders_value, quote=True),
                    folders_summary=summary,
                    db_path_attr=html.escape(str(self.db_path or index.default_db_path())),
                    app_version_attr=html.escape(self.app_version),
                    body="\n".join(body_parts),
                    folder_picker_script=FOLDER_PICKER_SCRIPT,
                    stream_script="",
                )
                self._send(200, page)
                return
            if type_selection != "all":
                allowed = {"." + t.lower() for t in types}
                results = [r for r in results if r[0].suffix.lower() in allowed]
            if folders_filtered:
                results = [
                    r for r in results
                    if any(_path_under(_normpath(str(r[0])), f) for f in effective_folders)
                ]
            inline_html, doc_count, hit_count = _render_inline(results, expr, limit)
            body_parts.append(
                f'<div class="meta"><span id="result-count" '
                f'data-docs="{doc_count}" data-hits="{hit_count}">'
                f"{doc_count} document(s), {hit_count} match(es)</span></div>"
            )
            body_parts.append('<div class="indexing" id="indexing" style="display:none"></div>')
            body_parts.append(f'<div id="results">{inline_html}</div>')

            stream_script = STREAM_SCRIPT_TPL.format(
                q_json=json.dumps(q),
                types_json=json.dumps(",".join(types)),
                folders_json=json.dumps("\n".join(effective_folders) if folders_filtered else ""),
                mode_json=json.dumps(mode),
                limit=limit,
            )
            title = f"{q} — docsearch"
        else:
            shown = effective_folders if folders_filtered else self.cfg["folders"]
            body_parts.append(
                '<div class="empty">Type a query above. '
                "Searching in: <br><br>"
                + "<br>".join(html.escape(f) for f in shown)
                + "</div>"
            )
            title = "docsearch"

        if folders_filtered:
            summary = "Folders: " + ", ".join(
                html.escape(os.path.basename(f) or f) for f in effective_folders
            ) + ' <a href="#" id="folders-reset">(reset)</a>'
        else:
            summary = "Folders: all configured"
        folders_value = "\n".join(effective_folders) if folders_filtered else ""

        db_path = str(self.db_path or index.default_db_path())
        page = PAGE.format(
            title=html.escape(title),
            q_attr=html.escape(q, quote=True),
            types_options=self._render_types_options(type_selection),
            mode_all_sel=' selected' if mode == "all" else "",
            mode_phrase_sel=' selected' if mode == "phrase" else "",
            mode_any_sel=' selected' if mode == "any" else "",
            folders_attr=html.escape(folders_value, quote=True),
            folders_summary=summary,
            db_path_attr=html.escape(db_path),
            app_version_attr=html.escape(self.app_version),
            body="\n".join(body_parts),
            folder_picker_script=FOLDER_PICKER_SCRIPT,
            stream_script=stream_script,
        )
        self._send(200, page)

    # /stream — SSE
    def _handle_stream(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        types_str = qs.get("types", [""])[0]
        if types_str.strip().lower() in ("", "all"):
            types = list(self.cfg["types"])
        else:
            types = [t.strip().lstrip(".") for t in types_str.split(",") if t.strip()] or list(self.cfg["types"])
        folders_raw = qs.get("folders", [""])[0]
        effective_folders, _ = _parse_folders_param(folders_raw, self.cfg["folders"])
        mode = qs.get("mode", [self.cfg.get("mode") or "all"])[0]
        if mode not in ("all", "phrase", "any"):
            mode = "all"
        try:
            limit = max(0, int(qs.get("limit", ["5"])[0]))
        except ValueError:
            limit = 5

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            self._stream_loop(q, types, limit, mode, effective_folders)
        except BrokenPipeError:
            return

    def _emit(self, event: str, data: dict) -> bool:
        chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _stream_loop(
        self,
        q: str,
        types: list[str],
        limit: int,
        mode: str = "all",
        folders: list[str] | None = None,
    ):
        # Open a reader DB for walk_unindexed enumeration.
        walk_folders = folders if folders else self.cfg["folders"]
        db = self._open_db()
        try:
            pending = list(index.walk_unindexed(db, walk_folders, types))
        finally:
            db.close()

        total = len(pending)
        if not self._emit("progress", {"total": total, "done": 0}):
            return
        if total == 0 or not q.strip():
            self._emit("done", {"total": total})
            return

        expr = index.build_match_expr(q, mode)
        done = 0
        done_lock = threading.Lock()
        send_lock = threading.Lock()  # serialise SSE writes to wfile
        # Single shared write connection + lock so concurrent extract threads
        # never contend for the DB write lock (the root cause of the
        # "database is locked" OperationalError under ThreadPoolExecutor).
        write_db = self._open_db()
        write_lock = threading.Lock()

        def worker(path: Path):
            nonlocal done
            # Extraction is the slow step (pdftotext, textutil). Run it
            # without any lock so all four threads can work in parallel.
            try:
                mtime, size, text, breaks = index.extract_content(path)
            except OSError:
                mtime = size = 0
                text = breaks = None

            # DB writes and the subsequent FTS5 search are fast; serialise
            # them through the single shared connection to eliminate contention.
            snippets = None
            with write_lock:
                try:
                    index.write_file(write_db, path, mtime, size, text, breaks)
                    if text is not None:
                        snippets = index.search_one(write_db, q, path, mode=mode)
                except Exception as exc:
                    sys.stderr.write(f"[docsearch] error indexing {path}: {exc}\n")

            with done_lock:
                done += 1
                d = done
            with send_lock:
                if snippets:
                    html_blob = _render_result_html(path, snippets, expr, limit)
                    self._emit("result", {
                        "path": str(path),
                        "snippets": snippets,
                        "html": html_blob,
                    })
                self._emit("progress", {"total": total, "done": d})

        # Bounded thread pool; pdftotext is the slow part.
        try:
            with ThreadPoolExecutor(max_workers=MAX_EXTRACTORS) as ex:
                list(ex.map(worker, pending))
        finally:
            write_db.close()

        self._emit("done", {"total": total})


def _write_token_file(token: str) -> Path:
    """Persist the session token at ~/.cache/docsearch/auth-token with mode
    0600 so only the owning user can read it. Returns the path written."""
    cache_dir = Path(os.environ.get(
        "DOCSEARCH_CACHE_DIR",
        str(Path("~/.cache/docsearch").expanduser()),
    ))
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / AUTH_TOKEN_FILENAME
    # Open with O_CREAT|O_WRONLY|O_TRUNC and an explicit mode so the file is
    # never world-readable, even briefly.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    # In case the file pre-existed with looser perms.
    os.chmod(path, 0o600)
    return path


def _git_version() -> str:
    """Return the short commit hash of HEAD, or 'dev' if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=Path(__file__).resolve().parent,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "dev"


def main():
    Handler.cfg = load_config()
    Handler.db_path = index.default_db_path()
    Handler.app_version = _git_version()
    Handler.session_token = secrets.token_urlsafe(32)
    token_path = _write_token_file(Handler.session_token)

    requested_port = int(os.environ.get("DOCSEARCH_PORT", 0))
    server = ThreadingHTTPServer(("127.0.0.1", requested_port), Handler)
    Handler.bound_port = server.server_address[1]

    base = f"http://127.0.0.1:{Handler.bound_port}"
    launch_url = f"{base}/?token={Handler.session_token}"
    print(f"docsearch web UI running at {base}/")
    print(f"version:    {Handler.app_version}")
    print(f"folders:    {Handler.cfg['folders']}")
    print(f"types:      {Handler.cfg['types']}")
    print(f"db:         {Handler.db_path}")
    print(f"auth token: {token_path} (mode 0600 — keep private)")
    print("Ctrl-C to stop.")
    try:
        subprocess.Popen(["open", launch_url])
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()

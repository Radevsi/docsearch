"""Local web UI for docsearch.

Architecture:
  GET /          → renders shell + indexed results (instant FTS5 query).
  GET /stream    → SSE: walks unindexed files, extracts in a thread pool,
                   streams matches as `result` events, persists to the index.
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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import index
from .config import load_config

MAX_EXTRACTORS = 4
SESSION_COOKIE = "docsearch_session"
AUTH_TOKEN_FILENAME = "auth-token"

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
</style>
</head>
<body>
<header>
  <h1><a href="/">docsearch</a></h1>
  <form method="get" action="/">
    <input type="text" name="q" value="{q_attr}" placeholder="Search your documents…" autofocus>
    <select name="mode" aria-label="Match mode">
      <option value="all"{mode_all_sel}>all words</option>
      <option value="phrase"{mode_phrase_sel}>exact phrase</option>
      <option value="any"{mode_any_sel}>any word</option>
    </select>
    <input type="hidden" name="types" value="{types_attr}">
    <button type="submit">Search</button>
  </form>
</header>
{body}
{stream_script}
</body>
</html>
"""

STREAM_SCRIPT_TPL = """
<script>
(function() {{
  const q = {q_json};
  const types = {types_json};
  const mode = {mode_json};
  const limit = {limit};
  const params = new URLSearchParams({{q: q, types: types, mode: mode, limit: String(limit)}});
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
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
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
        elif parsed.path == "/open":
            self._handle_open(parsed)
        else:
            self._send(404, "not found")

    # /open
    def _handle_open(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        p = qs.get("path", [""])[0]
        if p and Path(p).exists():
            subprocess.Popen(["open", p])
            self._send(200, "<script>history.back()</script>opening…")
        else:
            self._send(404, "not found")

    # /
    def _handle_search(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        types_str = qs.get("types", [",".join(self.cfg["types"])])[0]
        types = [t.strip().lstrip(".") for t in types_str.split(",") if t.strip()] or self.cfg["types"]
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
            finally:
                db.close()
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
                mode_json=json.dumps(mode),
                limit=limit,
            )
            title = f"{q} — docsearch"
        else:
            body_parts.append(
                '<div class="empty">Type a query above. '
                "Searching in: <br><br>"
                + "<br>".join(html.escape(f) for f in self.cfg["folders"])
                + "</div>"
            )
            title = "docsearch"

        page = PAGE.format(
            title=html.escape(title),
            q_attr=html.escape(q, quote=True),
            types_attr=html.escape(types_str, quote=True),
            mode_all_sel=' selected' if mode == "all" else "",
            mode_phrase_sel=' selected' if mode == "phrase" else "",
            mode_any_sel=' selected' if mode == "any" else "",
            body="\n".join(body_parts),
            stream_script=stream_script,
        )
        self._send(200, page)

    # /stream — SSE
    def _handle_stream(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        types_str = qs.get("types", [",".join(self.cfg["types"])])[0]
        types = [t.strip().lstrip(".") for t in types_str.split(",") if t.strip()] or self.cfg["types"]
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
            self._stream_loop(q, types, limit, mode)
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

    def _stream_loop(self, q: str, types: list[str], limit: int, mode: str = "all"):
        # Open a reader DB for walk_unindexed enumeration.
        db = self._open_db()
        try:
            pending = list(index.walk_unindexed(db, self.cfg["folders"], types))
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
        send_lock = threading.Lock()  # serialize writes to wfile

        def worker(path: Path):
            nonlocal done
            wdb = self._open_db()
            try:
                status = index.index_file(wdb, path)
                snippets = None
                if status == "ok":
                    snippets = index.search_one(wdb, q, path, mode=mode)
            finally:
                wdb.close()

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
        with ThreadPoolExecutor(max_workers=MAX_EXTRACTORS) as ex:
            list(ex.map(worker, pending))

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


def main():
    Handler.cfg = load_config()
    Handler.db_path = index.default_db_path()
    Handler.session_token = secrets.token_urlsafe(32)
    token_path = _write_token_file(Handler.session_token)

    requested_port = int(os.environ.get("DOCSEARCH_PORT", 0))
    server = ThreadingHTTPServer(("127.0.0.1", requested_port), Handler)
    Handler.bound_port = server.server_address[1]

    base = f"http://127.0.0.1:{Handler.bound_port}"
    launch_url = f"{base}/?token={Handler.session_token}"
    print(f"docsearch web UI running at {base}/")
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

"""Layer 4: SSE / web endpoint contract."""
import http.client
import json
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from docsearch import index, web


TEST_TOKEN = "test-session-token-fixed-for-determinism"


@contextmanager
def running_server(db_path, folders, types=("txt", "md")):
    web.Handler.cfg = {
        "folders": [str(f) for f in folders],
        "types": list(types),
        "mode": "all",
        "context": 80,
    }
    web.Handler.db_path = db_path
    web.Handler.session_token = TEST_TOKEN
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = srv.server_address[1]
    web.Handler.bound_port = port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)
        web.Handler.session_token = None
        web.Handler.bound_port = None


def _auth_headers(extra=None):
    h = {"Cookie": f"{web.SESSION_COOKIE}={TEST_TOKEN}"}
    if extra:
        h.update(extra)
    return h


def http_get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or _auth_headers())
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


def consume_sse(port, path, max_events=200, timeout=10, headers=None):
    """Iterate parsed (event, data_dict) pairs from an SSE endpoint."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path, headers=headers or _auth_headers())
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.headers.get("Content-Type", "").startswith("text/event-stream")

    event = None
    data_lines: list[str] = []
    count = 0
    fp = resp.fp
    try:
        while count < max_events:
            line = fp.readline()
            if not line:
                break
            s = line.decode("utf-8").rstrip("\r\n")
            if s == "":
                if event is not None:
                    payload = "\n".join(data_lines)
                    try:
                        data = json.loads(payload) if payload else {}
                    except json.JSONDecodeError:
                        data = {"_raw": payload}
                    yield event, data
                    count += 1
                    if event == "done":
                        break
                event, data_lines = None, []
            elif s.startswith("event: "):
                event = s[len("event: "):]
            elif s.startswith("data: "):
                data_lines.append(s[len("data: "):])
    finally:
        conn.close()


# ---------------------------------------------------------------------------

def test_search_endpoint_returns_indexed_results_inline(tmp_db, corpus):
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    db.close()

    with running_server(tmp_db, [corpus]) as port:
        status, body = http_get(port, "/?q=philosophy")
        assert status == 200
        assert "philosophy.txt" in body
        # The inline result count header is present.
        assert 'id="result-count"' in body


def test_stream_emits_done_when_nothing_to_index(tmp_db, corpus):
    # Pre-index everything so walk_unindexed yields nothing.
    db = index.open_db(tmp_db)
    for p in corpus.iterdir():
        if p.suffix in (".txt", ".md"):
            index.index_file(db, p)
    db.close()

    with running_server(tmp_db, [corpus]) as port:
        events = list(consume_sse(port, "/stream?q=philosophy&types=txt,md"))

    kinds = [e for e, _ in events]
    assert "done" in kinds
    # No 'result' events because everything was already searched against the
    # *inline* path, and the stream only walks unindexed files.
    assert "result" not in kinds


def test_stream_indexes_and_emits_result_for_match(tmp_db, corpus):
    # Empty DB, philosophy.txt is on disk but not indexed.
    index.open_db(tmp_db).close()

    with running_server(tmp_db, [corpus]) as port:
        events = list(consume_sse(port, "/stream?q=philosophy&types=txt,md"))

    results = [d for e, d in events if e == "result"]
    assert results, "expected at least one streamed result"
    paths = {r["path"] for r in results}
    assert any(p.endswith("philosophy.txt") for p in paths)

    # done event came at the end
    assert events[-1][0] == "done"


def test_stream_persists_index(tmp_db, corpus):
    """After streaming completes, files end up in the index so the next
    inline search hits them without re-extraction."""
    index.open_db(tmp_db).close()

    with running_server(tmp_db, [corpus]) as port:
        list(consume_sse(port, "/stream?q=philosophy&types=txt,md"))
        status, body = http_get(port, "/?q=philosophy")

    assert status == 200
    assert "philosophy.txt" in body

    # And the index actually has the file persisted.
    db = index.open_db(tmp_db)
    n = db.execute(
        "SELECT count(*) FROM files WHERE path LIKE ? AND status = 'ok'",
        (f"%philosophy.txt",),
    ).fetchone()[0]
    db.close()
    assert n == 1


def test_stream_emits_progress_counts(tmp_db, corpus):
    index.open_db(tmp_db).close()

    with running_server(tmp_db, [corpus]) as port:
        events = list(consume_sse(port, "/stream?q=philosophy&types=txt,md"))

    progress = [d for e, d in events if e == "progress"]
    assert progress, "expected progress events"
    # First progress carries `total`.
    assert progress[0].get("total", 0) > 0
    # Eventually done == total.
    last = progress[-1]
    assert last.get("done") == last.get("total")


def test_search_endpoint_honors_phrase_mode(tmp_db, corpus):
    """Phrase mode should pass `mode=phrase` through end-to-end: matches the
    contiguous phrase in mixed.txt but not philosophy.txt (only has 'philosophy')."""
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "mixed.txt")
    index.index_file(db, corpus / "philosophy.txt")
    db.close()

    with running_server(tmp_db, [corpus]) as port:
        status, body = http_get(port, "/?q=philosophy+of+art&mode=phrase")
        assert status == 200
        assert "mixed.txt" in body
        assert "philosophy.txt" not in body
        # The selected mode should be reflected in the form so the dropdown
        # round-trips after submission.
        assert 'value="phrase" selected' in body or '"phrase" selected' in body


def test_search_endpoint_sets_csp_header(tmp_db, corpus):
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    db.close()

    with running_server(tmp_db, [corpus]) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/?q=philosophy", headers=_auth_headers())
        resp = conn.getresponse()
        csp = resp.headers.get("Content-Security-Policy", "")
        resp.read()
        conn.close()
    assert "default-src 'none'" in csp
    # connect-src 'self' is required so the in-page fetch('/dirs') and the
    # SSE EventSource('/stream') aren't blocked by the default-src 'none'.
    assert "connect-src 'self'" in csp


# --- auth gate --------------------------------------------------------------

def test_unauthenticated_request_rejected(tmp_db, corpus):
    """Bare GET with no cookie and no token query returns 401."""
    with running_server(tmp_db, [corpus]) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/?q=philosophy")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 401


def test_token_query_sets_cookie_and_redirects(tmp_db, corpus):
    """The launch URL (with ?token=…) returns 302 + a Set-Cookie header
    carrying the session cookie, and the Location strips the token."""
    with running_server(tmp_db, [corpus]) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/?q=philosophy&token={TEST_TOKEN}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert f"{web.SESSION_COOKIE}={TEST_TOKEN}" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    location = resp.headers.get("Location", "")
    assert "token=" not in location
    assert "q=philosophy" in location


def test_cookie_grants_access(tmp_db, corpus):
    """Once the cookie is set, subsequent requests succeed."""
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    db.close()
    with running_server(tmp_db, [corpus]) as port:
        status, body = http_get(port, "/?q=philosophy")
    assert status == 200
    assert "philosophy.txt" in body


def test_wrong_token_rejected(tmp_db, corpus):
    """A mismatched token query returns 401, not a cookie."""
    with running_server(tmp_db, [corpus]) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/?q=philosophy&token=wrong-token")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 401
    assert "Set-Cookie" not in {k for k, _ in resp.getheaders()}


def test_cross_origin_request_rejected(tmp_db, corpus):
    """A request with a valid cookie but a foreign Origin header is 403."""
    with running_server(tmp_db, [corpus]) as port:
        headers = _auth_headers({"Origin": "http://evil.example"})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/?q=philosophy", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 403


def test_open_endpoint_requires_auth(tmp_db, corpus):
    """The /open endpoint, which Popens `open <path>`, is gated too."""
    with running_server(tmp_db, [corpus]) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/open?path=/tmp/anything")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 401


def test_stream_handles_extraction_failure(tmp_db, corpus):
    """A corrupt PDF in the folder shouldn't crash the stream."""
    index.open_db(tmp_db).close()

    with running_server(tmp_db, [corpus], types=("txt", "md", "pdf")) as port:
        events = list(consume_sse(port, "/stream?q=philosophy&types=txt,md,pdf"))

    # done event still arrives.
    assert events[-1][0] == "done"

    # corrupt.pdf is now recorded as failed.
    db = index.open_db(tmp_db)
    row = db.execute(
        "SELECT status FROM files WHERE path LIKE ?",
        ("%corrupt.pdf",),
    ).fetchone()
    db.close()
    assert row is not None
    # Either 'failed' (pdftotext installed and returned non-zero) or, if
    # pdftotext is unavailable, the extractor returns (None, None) and we
    # record 'failed' too because the suffix is in EXTRACTORS.
    assert row[0] in ("failed",)

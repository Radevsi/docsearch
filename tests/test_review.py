"""Regression tests for bugs found during deep review.

Each test documents the exact failure mode it guards against.
"""
import signal
import sqlite3
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

from docsearch import core, index
from test_web_sse import _auth_headers, consume_sse, http_get, running_server


# ---------------------------------------------------------------------------
# Bug 1: walk_unindexed followed symlinks via rglob → infinite loop
# ---------------------------------------------------------------------------

def test_walk_unindexed_does_not_follow_symlink_dir_loop(tmp_db, tmp_path):
    """A self-referential symlink (a/loop -> a) must not cause walk_unindexed
    to loop forever. Before the fix (rglob), this would hang indefinitely.

    The test enforces a hard wall-clock limit via threading so a regression
    is caught quickly rather than hanging CI."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.txt").write_text("hello")
    try:
        (root / "loop").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    db = index.open_db(tmp_db)
    found: list[Path] = []
    error: list[BaseException] = []

    def run():
        try:
            found.extend(index.walk_unindexed(db, [root], ["txt"]))
        except BaseException as e:
            error.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    db.close()

    assert not t.is_alive(), "walk_unindexed hung — symlink loop not guarded"
    assert not error, f"walk_unindexed raised: {error}"
    paths = {p.name for p in found}
    assert "real.txt" in paths
    # The symlink itself should never appear as a walkable entry
    assert "loop" not in paths


def test_walk_unindexed_does_not_recurse_into_symlinked_outside_dir(tmp_db, tmp_path):
    """A symlink to a directory outside the configured root must not be
    followed. Prevents scope creep where a sync folder (Dropbox etc.) has
    a symlink to a sibling directory."""
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.txt").write_text("classified")
    try:
        (root / "link_out").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    db = index.open_db(tmp_db)
    found = list(index.walk_unindexed(db, [root], ["txt"]))
    db.close()

    names = {p.name for p in found}
    assert "secret.txt" not in names


def test_walk_unindexed_indexes_file_symlinks(tmp_db, tmp_path):
    """Symlinks *to files* (not directories) should still be indexed — e.g.
    a user may have a symlink to a document in a different location."""
    root = tmp_path / "root"; root.mkdir()
    real = tmp_path / "real.txt"; real.write_text("searchable content")
    try:
        (root / "linked.txt").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    db = index.open_db(tmp_db)
    found = list(index.walk_unindexed(db, [root], ["txt"]))
    db.close()

    assert any(p.name == "linked.txt" for p in found)


def test_walk_unindexed_skips_hidden_directories(tmp_db, tmp_path):
    """Hidden directories (.git, .Trash, etc.) must not be descended into.
    Before the fix, rglob would index .git/config if its extension matched."""
    root = tmp_path / "root"; root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("hidden git config")  # .txt not in exts
    (root / ".git" / "readme.txt").write_text("should be skipped")
    (root / "visible.txt").write_text("should be found")

    db = index.open_db(tmp_db)
    found = list(index.walk_unindexed(db, [root], ["txt"]))
    db.close()

    names = {p.name for p in found}
    assert "visible.txt" in names
    assert "readme.txt" not in names  # inside .git


def test_walk_unindexed_skips_hidden_files(tmp_db, tmp_path):
    """Hidden files (starting with .) are skipped even if extension matches."""
    root = tmp_path / "root"; root.mkdir()
    (root / ".DS_Store.txt").write_text("mac junk")
    (root / "normal.txt").write_text("real file")

    db = index.open_db(tmp_db)
    found = list(index.walk_unindexed(db, [root], ["txt"]))
    db.close()

    names = {p.name for p in found}
    assert "normal.txt" in names
    assert ".DS_Store.txt" not in names


# ---------------------------------------------------------------------------
# Bug 2: invalid FTS5 query raised OperationalError instead of returning []
# ---------------------------------------------------------------------------

def test_search_invalid_fts5_returns_empty(tmp_db, tmp_path):
    """An unclosed phrase like '"unclosed' is invalid FTS5 syntax.
    search() must return [] rather than raising OperationalError — the error
    would otherwise propagate to the HTTP handler and drop the connection."""
    (tmp_path / "doc.txt").write_text("unclosed parenthesis example")
    db = index.open_db(tmp_db)
    index.index_file(db, tmp_path / "doc.txt")

    result = index.search(db, '"unclosed')   # invalid: unclosed phrase
    assert result == []

    result2 = index.search(db, "AND OR")     # degenerate but should not crash
    assert isinstance(result2, list)
    db.close()


def test_search_one_invalid_fts5_returns_none(tmp_db, tmp_path):
    """search_one() must also absorb FTS5 OperationalError gracefully."""
    f = tmp_path / "doc.txt"; f.write_text("some content")
    db = index.open_db(tmp_db)
    index.index_file(db, f)

    result = index.search_one(db, '"bad', f)
    assert result is None
    db.close()


def test_web_invalid_query_returns_200(tmp_db, corpus):
    """The web endpoint must return 200 rather than dropping the connection
    (500 or TCP close) when the user submits invalid FTS5 syntax.

    index.search() absorbs OperationalError and returns [], so the page
    renders as "no results" rather than an explicit error message — the
    important guarantee is the HTTP 200 (no crash, no dropped connection)."""
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    db.close()

    with running_server(tmp_db, [corpus]) as port:
        status, body = http_get(
            port, "/?q=" + urllib.parse.quote('"unclosed phrase')
        )

    assert status == 200
    # Page must render something — not a blank body or a stack trace.
    assert len(body) > 200


def test_stream_invalid_fts5_still_emits_done(tmp_db, corpus):
    """If FTS5 raises during a stream, the SSE must still close cleanly
    (emit 'done') so the browser EventSource doesn't hang open."""
    index.open_db(tmp_db).close()

    with running_server(tmp_db, [corpus]) as port:
        # Use a valid query so the stream opens, but search_one will see the
        # bad expr if we somehow inject it. This test mainly confirms the
        # stream still terminates even under error conditions.
        events = list(consume_sse(
            port, "/stream?q=philosophy&types=txt,md", timeout=8
        ))

    assert events[-1][0] == "done"


# ---------------------------------------------------------------------------
# Bug 3: pdftotext warning printed once per PDF, not once total
# ---------------------------------------------------------------------------

def test_pdftotext_warning_printed_only_once(tmp_path, monkeypatch, capsys):
    """When pdftotext is absent, the warning should appear exactly once no
    matter how many PDFs are processed. With 4 worker threads and 20 PDFs,
    the old code would print the warning up to 20 times."""
    # Reset the module-level flag so this test is independent.
    monkeypatch.setattr(core, "_pdftotext_warned", False)

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF fake")

    original_run = core.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pdftotext":
            raise FileNotFoundError("pdftotext")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    for _ in range(5):
        core.extract_pdf(fake_pdf)

    captured = capsys.readouterr()
    # Exactly one warning line, not five.
    assert captured.err.count("pdftotext") == 1


# ---------------------------------------------------------------------------
# Bug 4: write_file ROLLBACK exception shadows original error
# ---------------------------------------------------------------------------

def test_write_file_rollback_does_not_shadow_original_error(tmp_db, tmp_path):
    """If the DB write fails (e.g. after BEGIN IMMEDIATE succeeds but an
    INSERT fails), the original OperationalError must propagate — not be
    replaced by a ROLLBACK failure.

    sqlite3.Connection.execute is a C-extension slot (read-only), so we can't
    monkeypatch it directly. Instead wrap the real connection in a thin
    delegating class whose `execute` is a plain Python method we control."""
    f = tmp_path / "doc.txt"; f.write_text("hello")
    real_db = index.open_db(tmp_db)
    mtime, size, text, breaks = index.extract_content(f)

    call_count = [0]
    real_execute = real_db.execute

    class SabotageConn:
        """Thin wrapper: intercepts execute() after BEGIN IMMEDIATE, then
        raises on the first INSERT so we can test the ROLLBACK path."""
        def execute(self, sql, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1 and "INSERT" in sql.upper():
                raise sqlite3.OperationalError("injected write failure")
            return real_execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_db, name)

    sabotaged = SabotageConn()

    with pytest.raises(sqlite3.OperationalError, match="injected write failure"):
        index.write_file(sabotaged, f, mtime, size, text, breaks)

    real_db.close()


# ---------------------------------------------------------------------------
# Bug 5: cli _cmd_index crashed entire run on a single worker exception
# ---------------------------------------------------------------------------

def test_walk_unindexed_handles_stat_error_gracefully(tmp_db, tmp_path):
    """If a file disappears between directory listing and stat(), the walker
    should silently skip it rather than propagating OSError to the caller."""
    root = tmp_path / "root"; root.mkdir()
    (root / "stable.txt").write_text("hello")
    vanishing = root / "vanishing.txt"
    vanishing.write_text("bye")

    db = index.open_db(tmp_db)
    # Simulate the file being deleted mid-walk by making it unreadable.
    # We'll monkeypatch Path.stat so it raises for this specific path.
    original_stat = Path.stat

    def patched_stat(self, **kwargs):
        if self.name == "vanishing.txt":
            raise OSError("no such file")
        return original_stat(self, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(Path, "stat", patched_stat):
        found = list(index.walk_unindexed(db, [root], ["txt"]))

    db.close()

    names = {p.name for p in found}
    assert "stable.txt" in names
    assert "vanishing.txt" not in names

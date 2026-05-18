"""Tests for the folder-picker:

- unit tests for the path helpers (_normpath, _path_under, _resolve_within_roots,
  _dedupe_descendants, _parse_folders_param)
- integration tests for /dirs (auth, escape rejection, symlink-loop safety, cap)
- integration tests for the folder filter wired into / and /stream

The symlink loop tests are the important defensive ones: a self-referential
symlink (`a/loop -> a`) would let a naive picker descend forever. We verify
/dirs lists `loop` zero times and never follows it.
"""
import http.client
import json
import os
import shutil
import time
import urllib.parse
from pathlib import Path

import pytest

from docsearch import index, web

from test_web_sse import (
    TEST_TOKEN,
    _auth_headers,
    consume_sse,
    http_get,
    running_server,
)


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------

def test_normpath_collapses_traversal():
    """`..` segments resolve away. This is what stops `/root/../etc` from
    being treated as inside /root."""
    assert web._normpath("/a/b/../c") == "/a/c"
    assert web._normpath("/a//b") == "/a/b"


def test_path_under_requires_sep_boundary():
    """/foo must not be considered a parent of /foobar."""
    assert web._path_under("/a/b", "/a")
    assert web._path_under("/a", "/a")
    assert not web._path_under("/abc", "/a")
    assert not web._path_under("/a", "/a/b")  # child is not under deeper path


def test_resolve_within_roots_accepts_inside(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    result = web._resolve_within_roots(str(sub), [str(tmp_path)])
    assert result == Path(str(sub))


def test_resolve_within_roots_rejects_outside(tmp_path):
    """Anything not lexically under a root is rejected, even if it exists."""
    other = tmp_path.parent / (tmp_path.name + "_sibling")
    other.mkdir()
    try:
        assert web._resolve_within_roots(str(other), [str(tmp_path)]) is None
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_resolve_within_roots_rejects_traversal_escape(tmp_path):
    """A path containing `..` that escapes the root is rejected, even though
    the raw prefix string starts with the root."""
    escape = str(tmp_path) + "/../" + tmp_path.name + "_NOPE"
    assert web._resolve_within_roots(escape, [str(tmp_path)]) is None


def test_resolve_within_roots_rejects_nonexistent(tmp_path):
    """Path inside root but doesn't exist → None."""
    assert web._resolve_within_roots(str(tmp_path / "ghost"), [str(tmp_path)]) is None


def test_resolve_within_roots_rejects_file_not_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert web._resolve_within_roots(str(f), [str(tmp_path)]) is None


def test_dedupe_descendants_parent_wins():
    """If both parent and child are selected, the parent absorbs the child."""
    out = web._dedupe_descendants(["/a", "/a/b", "/a/b/c", "/d"])
    assert out == ["/a", "/d"]


def test_dedupe_descendants_no_overlap():
    out = web._dedupe_descendants(["/x", "/y", "/z"])
    assert out == ["/x", "/y", "/z"]


def test_dedupe_descendants_capped():
    """Defensive cap: pathological input can't explode the selection list."""
    huge = [f"/r/{i}" for i in range(web.MAX_FOLDER_SELECTIONS + 50)]
    out = web._dedupe_descendants(huge)
    assert len(out) == web.MAX_FOLDER_SELECTIONS


def test_parse_folders_unfiltered_when_empty():
    folders, filtered = web._parse_folders_param("", ["/root"])
    assert filtered is False
    assert folders == ["/root"]


def test_parse_folders_silently_drops_outside_paths():
    """A hand-crafted URL with a path outside the roots gets dropped, and if
    none remain we fall back to unfiltered."""
    folders, filtered = web._parse_folders_param("/etc", ["/Users/me/Docs"])
    assert filtered is False
    assert folders == ["/Users/me/Docs"]


def test_parse_folders_dedupes_descendants():
    folders, filtered = web._parse_folders_param(
        "/root/a\n/root/a/sub\n/root/b", ["/root"]
    )
    assert filtered is True
    assert folders == ["/root/a", "/root/b"]


def test_parse_folders_accepts_comma_or_newline():
    """Tolerate either separator since URL-encoded newlines are awkward."""
    folders, filtered = web._parse_folders_param("/root/a,/root/b", ["/root"])
    assert filtered is True
    assert folders == ["/root/a", "/root/b"]


# ---------------------------------------------------------------------------
# /dirs integration — auth and listing
# ---------------------------------------------------------------------------

def test_dirs_requires_auth(tmp_db, corpus):
    with running_server(tmp_db, [corpus]) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/dirs?path=" + urllib.parse.quote(str(corpus)))
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 401


def test_dirs_empty_path_returns_configured_roots(tmp_db, tmp_path):
    a = tmp_path / "rootA"; a.mkdir()
    b = tmp_path / "rootB"; b.mkdir()
    (a / "sub1").mkdir()  # so has_children is true for rootA
    with running_server(tmp_db, [a, b]) as port:
        status, body = http_get(port, "/dirs?path=")
    assert status == 200
    data = json.loads(body)
    assert data["path"] == ""
    paths = [c["path"] for c in data["children"]]
    assert str(a) in paths and str(b) in paths
    by_path = {c["path"]: c for c in data["children"]}
    assert by_path[str(a)]["has_children"] is True
    assert by_path[str(b)]["has_children"] is False


def test_dirs_lists_subdirs_alphabetically(tmp_db, tmp_path):
    root = tmp_path / "root"; root.mkdir()
    for name in ("Charlie", "alpha", "Bravo"):
        (root / name).mkdir()
    (root / "file.txt").write_text("x")  # file, must not appear
    with running_server(tmp_db, [root]) as port:
        status, body = http_get(port, "/dirs?path=" + urllib.parse.quote(str(root)))
    assert status == 200
    data = json.loads(body)
    names = [c["name"] for c in data["children"]]
    assert names == sorted(names, key=str.lower)
    assert "file.txt" not in names


def test_dirs_skips_hidden_entries(tmp_db, tmp_path):
    root = tmp_path / "root"; root.mkdir()
    (root / ".hidden").mkdir()
    (root / "visible").mkdir()
    with running_server(tmp_db, [root]) as port:
        status, body = http_get(port, "/dirs?path=" + urllib.parse.quote(str(root)))
    data = json.loads(body)
    names = [c["name"] for c in data["children"]]
    assert "visible" in names
    assert ".hidden" not in names


def test_dirs_rejects_path_outside_roots(tmp_db, tmp_path):
    """A hand-crafted GET to /dirs with an unauthorized path → 403."""
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "elsewhere"; outside.mkdir()
    with running_server(tmp_db, [root]) as port:
        status, _ = http_get(
            port, "/dirs?path=" + urllib.parse.quote(str(outside))
        )
    assert status == 403


def test_dirs_rejects_traversal_escape(tmp_db, tmp_path):
    """`/root/../sibling` normalizes outside /root → 403."""
    root = tmp_path / "root"; root.mkdir()
    sibling = tmp_path / "sibling"; sibling.mkdir()
    bad = str(root) + "/../sibling"
    with running_server(tmp_db, [root]) as port:
        status, _ = http_get(port, "/dirs?path=" + urllib.parse.quote(bad))
    assert status == 403


def test_dirs_does_not_follow_symlink_loop(tmp_db, tmp_path):
    """A self-referential symlink (`a/loop -> a`) must NOT appear as a child
    of `a`. This is the loop-safety guarantee: the picker can never descend
    into infinite recursion because /dirs uses follow_symlinks=False and
    returns 0 children for the link.

    A naive implementation using is_dir() (which follows symlinks) would list
    `loop` as a navigable subdir; clicking it would return `a`'s children
    forever."""
    root = tmp_path / "root"; root.mkdir()
    a = root / "a"; a.mkdir()
    (a / "real_sub").mkdir()
    loop = a / "loop"
    try:
        loop.symlink_to(a, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")

    with running_server(tmp_db, [root]) as port:
        status, body = http_get(port, "/dirs?path=" + urllib.parse.quote(str(a)))
    assert status == 200
    names = [c["name"] for c in json.loads(body)["children"]]
    assert "real_sub" in names
    assert "loop" not in names  # symlink to dir is NOT listed


def test_dirs_does_not_follow_symlink_to_outside(tmp_db, tmp_path):
    """Even a non-loop symlink pointing OUTSIDE the configured roots is not
    listed. We don't want clicks on such a link to either succeed (escape)
    or break (broken UI)."""
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")

    with running_server(tmp_db, [root]) as port:
        status, body = http_get(port, "/dirs?path=" + urllib.parse.quote(str(root)))
    data = json.loads(body)
    names = [c["name"] for c in data["children"]]
    assert "link" not in names


def test_dirs_caps_huge_directory(tmp_db, tmp_path, monkeypatch):
    """A wide directory with many children is capped at MAX_DIR_CHILDREN and
    signals `truncated`. Without this, a folder with hundreds of thousands of
    subdirs could turn one click into a multi-megabyte JSON payload."""
    monkeypatch.setattr(web, "MAX_DIR_CHILDREN", 10)
    root = tmp_path / "root"; root.mkdir()
    for i in range(25):
        (root / f"d{i:02d}").mkdir()
    with running_server(tmp_db, [root]) as port:
        status, body = http_get(port, "/dirs?path=" + urllib.parse.quote(str(root)))
    data = json.loads(body)
    assert len(data["children"]) == 10
    assert data["truncated"] is True


def test_dirs_handles_deeply_nested_path(tmp_db, tmp_path):
    """Sanity check: walking ten levels deep through legitimate dirs works
    because we descend one level per request — no recursion on the server."""
    root = tmp_path / "root"; root.mkdir()
    cur = root
    for i in range(10):
        cur = cur / f"level{i}"
        cur.mkdir()

    with running_server(tmp_db, [root]) as port:
        path = str(root)
        # 10 nested dirs → 10 descent steps each yielding one child, then the
        # leaf has none.
        for _ in range(10):
            status, body = http_get(
                port, "/dirs?path=" + urllib.parse.quote(path)
            )
            assert status == 200
            data = json.loads(body)
            assert len(data["children"]) == 1
            path = data["children"][0]["path"]
        status, body = http_get(port, "/dirs?path=" + urllib.parse.quote(path))
        assert json.loads(body)["children"] == []


# ---------------------------------------------------------------------------
# Folder filter — wired into search
# ---------------------------------------------------------------------------

def test_search_filter_restricts_to_selected_folder(tmp_db, tmp_path):
    """When `folders=/root/a` is set, only docs under /root/a appear."""
    root = tmp_path / "root"; root.mkdir()
    a = root / "a"; a.mkdir()
    b = root / "b"; b.mkdir()
    (a / "doc_a.txt").write_text("philosophy is great")
    (b / "doc_b.txt").write_text("philosophy is also here")

    db = index.open_db(tmp_db)
    index.index_file(db, a / "doc_a.txt")
    index.index_file(db, b / "doc_b.txt")
    db.close()

    with running_server(tmp_db, [root]) as port:
        # No filter → both files appear.
        _, body_all = http_get(port, "/?q=philosophy")
        assert "doc_a.txt" in body_all
        assert "doc_b.txt" in body_all

        # Filter to /root/a → only doc_a appears.
        qs = "q=philosophy&folders=" + urllib.parse.quote(str(a))
        _, body_a = http_get(port, "/?" + qs)
        assert "doc_a.txt" in body_a
        assert "doc_b.txt" not in body_a


def test_search_filter_with_unknown_folder_falls_back_to_all(tmp_db, tmp_path):
    """A `folders=` value that has no overlap with configured roots is
    treated as 'no filter' rather than 'no results' — defensive UX."""
    root = tmp_path / "root"; root.mkdir()
    (root / "doc.txt").write_text("philosophy")
    db = index.open_db(tmp_db)
    index.index_file(db, root / "doc.txt")
    db.close()
    with running_server(tmp_db, [root]) as port:
        _, body = http_get(
            port, "/?q=philosophy&folders=" + urllib.parse.quote("/totally/elsewhere")
        )
    assert "doc.txt" in body


def test_stream_restricts_walk_to_selected_folder(tmp_db, tmp_path):
    """The SSE walk_unindexed pass should honor `folders=` and only walk the
    selected subtree, so newly-discovered hits outside that subtree are not
    streamed."""
    root = tmp_path / "root"; root.mkdir()
    a = root / "a"; a.mkdir()
    b = root / "b"; b.mkdir()
    (a / "doc_a.txt").write_text("philosophy in a")
    (b / "doc_b.txt").write_text("philosophy in b")

    index.open_db(tmp_db).close()  # empty index — both files are unindexed

    with running_server(tmp_db, [root]) as port:
        events = list(consume_sse(
            port,
            "/stream?q=philosophy&types=txt"
            "&folders=" + urllib.parse.quote(str(a)),
        ))

    results = [d for e, d in events if e == "result"]
    paths = {r["path"] for r in results}
    assert any(p.endswith("doc_a.txt") for p in paths)
    assert not any(p.endswith("doc_b.txt") for p in paths)


def test_search_filter_round_trips_in_form(tmp_db, tmp_path):
    """After submitting with `folders=`, the rendered page carries the same
    value in the hidden input so the next submission preserves it."""
    root = tmp_path / "root"; root.mkdir()
    a = root / "a"; a.mkdir()
    (a / "doc.txt").write_text("philosophy")
    db = index.open_db(tmp_db)
    index.index_file(db, a / "doc.txt")
    db.close()
    with running_server(tmp_db, [root]) as port:
        _, body = http_get(
            port, "/?q=philosophy&folders=" + urllib.parse.quote(str(a))
        )
    # The hidden input value is the selected folder path.
    assert f'id="folders-input" value="{str(a)}"' in body
    # Summary shows the basename.
    assert "Folders: " in body and Path(a).name in body

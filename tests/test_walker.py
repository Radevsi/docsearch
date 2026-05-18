"""Layer 2: filesystem ↔ index walker."""
import os
import time

import pytest

from docsearch import index


def test_walk_unindexed_finds_new_files(tmp_db, corpus):
    db = index.open_db(tmp_db)
    found = set(index.walk_unindexed(db, [corpus], ["txt", "md"]))
    names = {p.name for p in found}
    # Should include every txt/md but not the PDF or anything else.
    assert "philosophy.txt" in names
    assert "art.md" in names
    assert "mixed.txt" in names
    assert "cafe.txt" in names
    assert "multipage.txt" in names
    assert "corrupt.pdf" not in names


def test_walk_unindexed_skips_indexed(tmp_db, corpus):
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    found = {p.name for p in index.walk_unindexed(db, [corpus], ["txt", "md"])}
    assert "philosophy.txt" not in found
    assert "art.md" in found


def test_walk_unindexed_picks_up_mtime_change(tmp_db, corpus):
    db = index.open_db(tmp_db)
    target = corpus / "philosophy.txt"
    index.index_file(db, target)
    assert target.name not in {p.name for p in index.walk_unindexed(db, [corpus], ["txt"])}

    target.write_text("updated content")
    future = time.time() + 10
    os.utime(target, (future, future))

    assert target.name in {p.name for p in index.walk_unindexed(db, [corpus], ["txt"])}


def test_walk_unindexed_respects_type_filter(tmp_db, corpus):
    db = index.open_db(tmp_db)
    found = {p.suffix for p in index.walk_unindexed(db, [corpus], ["txt"])}
    assert found <= {".txt"}
    assert ".md" not in found


def test_walk_unindexed_skips_failed_unchanged(tmp_db, corpus):
    """A file recorded as failed (e.g. corrupt PDF) is not re-yielded
    unless its mtime/size changes."""
    db = index.open_db(tmp_db)
    assert index.index_file(db, corpus / "corrupt.pdf") == "failed"

    found = {p.name for p in index.walk_unindexed(db, [corpus], ["pdf"])}
    assert "corrupt.pdf" not in found


def test_walk_unindexed_unicode_paths(tmp_db, corpus):
    db = index.open_db(tmp_db)
    weird = corpus / "café notes.txt"
    weird.write_text("a note about café")
    found = {p.name for p in index.walk_unindexed(db, [corpus], ["txt"])}
    assert "café notes.txt" in found

    # And it can be indexed + searched.
    assert index.index_file(db, weird) == "ok"
    results = index.search(db, "café")
    assert any(p == weird for p, _ in results)

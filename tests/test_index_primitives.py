"""Layer 1: index primitives — schema, index_file, search."""
import os
import time

import pytest

from docsearch import index


# --- schema / lifecycle -----------------------------------------------------

def test_schema_idempotent(tmp_db):
    db1 = index.open_db(tmp_db)
    db1.close()
    db2 = index.open_db(tmp_db)
    db2.close()


def test_search_empty_index_returns_empty(tmp_db):
    db = index.open_db(tmp_db)
    assert index.search(db, "anything") == []


# --- single-file index + search --------------------------------------------

def test_index_then_search_txt(tmp_db, corpus):
    db = index.open_db(tmp_db)
    status = index.index_file(db, corpus / "philosophy.txt")
    assert status == "ok"

    results = index.search(db, "philosophy")
    assert len(results) == 1
    path, snippets = results[0]
    assert path == corpus / "philosophy.txt"
    assert len(snippets) >= 1


def test_multiple_matches_one_file(tmp_db, corpus):
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    results = index.search(db, "philosophy")
    _, snippets = results[0]
    # philosophy.txt contains the word at least 5 times
    assert len(snippets) >= 5


# --- query semantics --------------------------------------------------------

def test_phrase_vs_and(tmp_db, corpus):
    """mixed.txt contains 'philosophy of art' — phrase 'philosophy art' should NOT match;
    AND of both words should."""
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "mixed.txt")

    phrase = index.search(db, '"philosophy art"')
    assert phrase == [], "exact phrase should not match 'philosophy of art'"

    both = index.search(db, "philosophy AND art")
    assert len(both) == 1


def test_diacritic_insensitive(tmp_db, corpus):
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "cafe.txt")
    results = index.search(db, "cafe")
    assert len(results) == 1


def test_case_insensitive(tmp_db, corpus):
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    # "Philosophy" appears capitalized in the fixture; lowercase query should still match.
    assert index.search(db, "philosophy")
    assert index.search(db, "PHILOSOPHY")


# --- re-index behavior ------------------------------------------------------

def test_reindex_unchanged_mtime_is_noop(tmp_db, corpus):
    db = index.open_db(tmp_db)
    assert index.index_file(db, corpus / "art.md") == "ok"
    assert index.index_file(db, corpus / "art.md") == "unchanged"

    # One FTS row only
    n = db.execute("SELECT count(*) FROM docs WHERE path = ?", (str(corpus / "art.md"),)).fetchone()[0]
    assert n == 1


def test_reindex_after_mtime_change_replaces(tmp_db, corpus):
    db = index.open_db(tmp_db)
    target = corpus / "art.md"
    assert index.index_file(db, target) == "ok"
    assert index.search(db, "treatise")

    # Replace content and bump mtime
    target.write_text("# Music\n\nA short note on music. The art word is gone.\n")
    future = time.time() + 10
    os.utime(target, (future, future))

    assert index.index_file(db, target) == "ok"
    # Still one row
    n = db.execute("SELECT count(*) FROM docs WHERE path = ?", (str(target),)).fetchone()[0]
    assert n == 1
    # Old content no longer matches
    assert index.search(db, "treatise") == []
    # New content does
    assert index.search(db, "music")


# --- failure handling -------------------------------------------------------

def test_failed_extraction_recorded(tmp_db, corpus):
    db = index.open_db(tmp_db)
    status = index.index_file(db, corpus / "corrupt.pdf")
    assert status == "failed"

    row = db.execute(
        "SELECT status FROM files WHERE path = ?",
        (str(corpus / "corrupt.pdf"),),
    ).fetchone()
    assert row[0] == "failed"

    n = db.execute(
        "SELECT count(*) FROM docs WHERE path = ?",
        (str(corpus / "corrupt.pdf"),),
    ).fetchone()[0]
    assert n == 0


# --- page-break roundtrip ---------------------------------------------------

# --- build_match_expr + mode-aware search ----------------------------------

def test_build_match_expr_all():
    assert index.build_match_expr("philosophy art", "all") == "philosophy AND art"
    # single token unchanged
    assert index.build_match_expr("philosophy", "all") == "philosophy"


def test_build_match_expr_phrase():
    assert index.build_match_expr("philosophy of art", "phrase") == '"philosophy of art"'


def test_build_match_expr_any():
    assert index.build_match_expr("art history", "any") == "art OR history"


def test_build_match_expr_passthrough_when_raw_uses_fts5_syntax():
    # Quoted phrases, explicit boolean operators, and NEAR(...) pass through.
    assert index.build_match_expr('"foo bar"', "all") == '"foo bar"'
    assert index.build_match_expr("foo AND bar", "any") == "foo AND bar"
    assert index.build_match_expr("NEAR(foo bar, 5)", "all") == "NEAR(foo bar, 5)"
    # Lowercase 'and' is just a word, not an operator — should be tokenized.
    assert index.build_match_expr("foo and bar", "all") == "foo AND and AND bar"


def test_build_match_expr_empty_returns_empty():
    assert index.build_match_expr("", "all") == ""
    assert index.build_match_expr("   ", "phrase") == ""


def test_build_match_expr_strips_punctuation():
    # Punctuation isn't a token char — prevents FTS5 syntax injection via user input.
    assert index.build_match_expr("hello; world!", "all") == "hello AND world"


def test_search_phrase_mode_finds_adjacent_only(tmp_db, corpus):
    """Phrase mode should require contiguous terms.

    mixed.txt has 'philosophy of art' (contiguous) and 'philosophy and ... art' (not).
    art.md mentions 'art' but never 'philosophy'.
    Phrase 'philosophy art' should match neither.
    Phrase 'philosophy of art' should match only mixed.txt.
    """
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "mixed.txt")
    index.index_file(db, corpus / "art.md")

    assert index.search(db, "philosophy art", mode="phrase") == []

    results = index.search(db, "philosophy of art", mode="phrase")
    assert len(results) == 1
    assert results[0][0].name == "mixed.txt"


def test_search_any_mode_unions_terms(tmp_db, corpus):
    """`any` mode → OR. 'philosophy OR art' matches both philosophy.txt and art.md."""
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")
    index.index_file(db, corpus / "art.md")

    results = index.search(db, "philosophy art", mode="any")
    names = sorted(p.name for p, _ in results)
    assert names == ["art.md", "philosophy.txt"]


def test_search_all_mode_requires_every_term(tmp_db, corpus):
    """`all` mode → AND. Both terms must appear in the same doc."""
    db = index.open_db(tmp_db)
    index.index_file(db, corpus / "philosophy.txt")  # no 'art'
    index.index_file(db, corpus / "art.md")          # no 'philosophy'
    index.index_file(db, corpus / "mixed.txt")       # both

    results = index.search(db, "philosophy art", mode="all")
    assert [p.name for p, _ in results] == ["mixed.txt"]


def test_page_break_roundtrip(tmp_db, corpus, monkeypatch):
    """Stub the extractor to return known content with known page breaks,
    then assert search reports the correct page for a known hit."""
    text = "first page alpha word\fsecond page beta word\fthird page gamma word"
    breaks = [0]
    for i, ch in enumerate(text):
        if ch == "\f":
            breaks.append(i + 1)

    def fake_extract(path):
        return text, breaks

    monkeypatch.setattr(index, "_extract", fake_extract)

    db = index.open_db(tmp_db)
    fake_path = corpus / "fake.pdf"
    fake_path.write_bytes(b"placeholder")
    index.index_file(db, fake_path)

    results = index.search(db, "gamma")
    assert len(results) == 1
    _, snippets = results[0]
    assert snippets[0]["page"] == 3

    results = index.search(db, "beta")
    _, snippets = results[0]
    assert snippets[0]["page"] == 2

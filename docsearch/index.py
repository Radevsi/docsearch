"""Persistent FTS5 index for docsearch.

`open_db` opens (and migrates) the index. `index_file` extracts a file and
upserts it. `search` runs an FTS5 MATCH and returns hits with snippets +
page/line locations. `walk_unindexed` (Layer 2) yields filesystem paths
not yet — or no longer — represented in the index.
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import time
from pathlib import Path
from typing import Iterable, Iterator

from . import core


# --- extraction indirection --------------------------------------------------
# Indirection point so tests can monkeypatch a stub extractor.
def _extract(path: Path):
    return core.extract(path)


# --- DB lifecycle ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    page_breaks BLOB,
    status      TEXT NOT NULL,
    error       TEXT,
    indexed_at  REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    path UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def open_db(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(SCHEMA)
    return db


# --- page-break encoding -----------------------------------------------------

def _pack_breaks(breaks: list[int] | None) -> bytes | None:
    if not breaks:
        return None
    return struct.pack(f"<{len(breaks)}I", *breaks)


def _unpack_breaks(blob: bytes | None) -> list[int] | None:
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}I", blob))


# --- index a single file -----------------------------------------------------

def extract_content(path: Path) -> tuple[float, int, str | None, list[int] | None]:
    """Stat and extract `path` without touching the database.

    Returns (mtime, size, text, page_breaks). text is None when the format is
    unsupported or extraction fails. Safe to call concurrently from many
    threads — no shared mutable state."""
    path = Path(path)
    st = path.stat()
    text, breaks = _extract(path)
    return st.st_mtime, st.st_size, text, breaks


def write_file(
    db: sqlite3.Connection,
    path: Path,
    mtime: float,
    size: int,
    text: str | None,
    breaks: list[int] | None,
) -> str:
    """Persist pre-extracted content to the index and return a status string.

    Groups all writes in one BEGIN/COMMIT so a mid-write crash cannot leave
    the files and docs tables inconsistent. Must be called while the caller
    holds whatever lock serialises access to `db`."""
    path = Path(path)
    # BEGIN IMMEDIATE acquires the write lock upfront so the busy handler
    # (PRAGMA busy_timeout) fires here — a single statement — rather than
    # mid-transaction where SQLite does NOT invoke the busy handler.
    db.execute("BEGIN IMMEDIATE")
    try:
        if text is None:
            ext = path.suffix.lower().lstrip(".")
            status = "unsupported" if ext not in core.EXTRACTORS else "failed"
            db.execute(
                """INSERT INTO files(path, mtime, size, page_breaks, status, error, indexed_at)
                     VALUES(?, ?, ?, NULL, ?, NULL, ?)
                     ON CONFLICT(path) DO UPDATE SET
                       mtime=excluded.mtime, size=excluded.size,
                       status=excluded.status, error=NULL,
                       indexed_at=excluded.indexed_at, page_breaks=NULL""",
                (str(path), mtime, size, status, time.time()),
            )
            db.execute("DELETE FROM docs WHERE path = ?", (str(path),))
        elif (path.suffix.lower() == ".pdf"
              and len(text.strip()) < core.OCR_TEXT_THRESHOLD):
            # pdftotext ran but returned almost no text — likely a scanned PDF.
            # Queue for OCR rather than storing empty/useless content.
            status = "ocr_pending"
            db.execute(
                """INSERT INTO files(path, mtime, size, page_breaks, status, error, indexed_at)
                     VALUES(?, ?, ?, NULL, 'ocr_pending', NULL, ?)
                     ON CONFLICT(path) DO UPDATE SET
                       mtime=excluded.mtime, size=excluded.size,
                       status='ocr_pending', error=NULL,
                       indexed_at=excluded.indexed_at, page_breaks=NULL""",
                (str(path), mtime, size, time.time()),
            )
            db.execute("DELETE FROM docs WHERE path = ?", (str(path),))
        else:
            status = "ok"
            db.execute("DELETE FROM docs WHERE path = ?", (str(path),))
            db.execute(
                "INSERT INTO docs(path, content) VALUES(?, ?)", (str(path), text)
            )
            db.execute(
                """INSERT INTO files(path, mtime, size, page_breaks, status, error, indexed_at)
                     VALUES(?, ?, ?, ?, 'ok', NULL, ?)
                     ON CONFLICT(path) DO UPDATE SET
                       mtime=excluded.mtime, size=excluded.size,
                       page_breaks=excluded.page_breaks, status='ok',
                       error=NULL, indexed_at=excluded.indexed_at""",
                (str(path), mtime, size, _pack_breaks(breaks), time.time()),
            )
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return status


def index_file(db: sqlite3.Connection, path: Path) -> str:
    """Extract `path` and upsert it into the index.

    Convenience wrapper used by the CLI and tests. For the streaming web
    worker, prefer extract_content() + write_file() so extraction can run
    in parallel while writes are serialised under an external lock.

    Returns: 'ok' | 'failed' | 'unsupported' | 'unchanged'.
    """
    path = Path(path)
    st = path.stat()
    mtime, size = st.st_mtime, st.st_size
    row = db.execute(
        "SELECT mtime, size FROM files WHERE path = ?", (str(path),)
    ).fetchone()
    if row is not None and row[0] == mtime and row[1] == size:
        return "unchanged"
    _, _, text, breaks = extract_content(path)
    return write_file(db, path, mtime, size, text, breaks)


# --- search ------------------------------------------------------------------

def _page_of(offset: int, breaks: list[int]) -> int:
    # breaks is sorted ascending; bisect_right gives the index of the first
    # break *after* offset, which equals the 1-indexed page number.
    import bisect

    return max(1, bisect.bisect_right(breaks, offset))


def _line_offsets(content: str) -> list[int]:
    """Return a sorted list of character offsets where each line begins.
    Used with bisect to convert a char offset → line number in O(log N)
    instead of re-scanning content from the start for every match.
    """
    out = [0]
    i = content.find("\n")
    while i != -1:
        out.append(i + 1)
        i = content.find("\n", i + 1)
    return out


def default_db_path() -> Path:
    """Where the index lives. Override with $DOCSEARCH_DB."""
    env = os.environ.get("DOCSEARCH_DB")
    if env:
        return Path(env).expanduser()
    return Path("~/.cache/docsearch/index.sqlite").expanduser()


def search_one(
    db: sqlite3.Connection, query: str, path: Path, mode: str = "all"
) -> list[dict] | None:
    """Search a single indexed file. Returns its snippet list, or None if it
    isn't currently indexed or doesn't match the query."""
    expr = build_match_expr(query, mode)
    if not expr:
        return None
    try:
        row = db.execute(
            """SELECT d.content, f.page_breaks
                 FROM docs d JOIN files f ON f.path = d.path
                WHERE d.path = ? AND d.content MATCH ?""",
            (str(path), expr),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    content, breaks_blob = row
    breaks = _unpack_breaks(breaks_blob)
    snippets = _build_snippets(db, str(path), content, expr, breaks)
    return snippets or None


def search(
    db: sqlite3.Connection,
    query: str,
    limit: int = 200,
    mode: str = "all",
) -> list[tuple[Path, list[dict]]]:
    """Run FTS5 MATCH and return [(path, [snippet_record, ...]), ...] ordered by rank.

    snippet_record is a dict like {"page": 2, "text": "...philosophy..."} or
    {"line": 14, "text": "..."}; the text already has <mark>…</mark> highlights.
    """
    expr = build_match_expr(query, mode)
    if not expr:
        return []

    try:
        rows = db.execute(
            """SELECT d.path, d.content, f.page_breaks
                 FROM docs d JOIN files f ON f.path = d.path
                WHERE d.content MATCH ?
                ORDER BY bm25(docs)
                LIMIT ?""",
            (expr, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: list[tuple[Path, list[dict]]] = []
    for path_str, content, breaks_blob in rows:
        breaks = _unpack_breaks(breaks_blob)
        snippets = _build_snippets(db, path_str, content, expr, breaks)
        if snippets:
            out.append((Path(path_str), snippets))
    return out


def _fold(s: str) -> tuple[str, list[int] | None]:
    """Lowercase + strip diacritics. Returns (folded_str, idx_map).

    Fast path: when `s` is pure ASCII, idx_map is None — folded char i sits at
    original index i, so callers can skip the mapping list entirely. This
    matters for large docs where building a million-entry idx_map dominates.
    """
    if s.isascii():
        return s.lower(), None

    import unicodedata

    out_chars: list[str] = []
    out_idx: list[int] = []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            out_chars.append(c.lower())
            out_idx.append(i)
    return "".join(out_chars), out_idx


def _idx(idx_map: list[int] | None, i: int, fallback: int) -> int:
    """Map a folded-string offset back to the original string. With an ASCII
    fast-path (idx_map=None), offsets are identical."""
    if idx_map is None:
        return i if i < fallback else fallback
    return idx_map[i] if i < len(idx_map) else fallback


def _parse_match_expr(expr: str) -> tuple[bool, list[str]]:
    """Parse an FTS5 expression we built (or that a user typed) into
    (is_phrase, terms) for snippet building.

    A literal `"a b c"` (only one phrase, no boolean wrapping) is treated as a
    phrase so we can highlight contiguous matches. Anything else collapses to
    its bare tokens.
    """
    s = expr.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"' and s.count('"') == 2:
        inner = s[1:-1]
        return True, [t for t in re.findall(r"\w+", inner, flags=re.UNICODE)]
    return False, _query_terms(expr)


MAX_SNIPPETS_PER_DOC = 200


def _build_snippets(
    db: sqlite3.Connection,
    path_str: str,
    content: str,
    query: str,
    breaks: list[int] | None,
    context: int = 80,
    max_snippets: int = MAX_SNIPPETS_PER_DOC,
) -> list[dict]:
    """Find match offsets in `content` and produce snippet records.

    FTS5's tokenizer folds case + diacritics, so we mirror that here: search a
    folded copy of the content for folded query terms, then map offsets back
    to the original text for snippet/page resolution.

    Bounded work: at most `max_snippets` snippet records are built. The UI
    only ever shows a handful per file, so building 500k snippet dicts for a
    common term like 'the' is pure waste.
    """
    is_phrase, terms = _parse_match_expr(query)
    if not terms:
        return []

    folded_content, idx_map = _fold(content)
    folded_terms = [_fold(t)[0] for t in terms if t]
    folded_terms = [t for t in folded_terms if t]
    if not folded_terms:
        return []

    if is_phrase:
        pattern = re.compile(
            r"\b" + r"\s+".join(re.escape(t) for t in folded_terms) + r"\b"
        )
    else:
        # Match longer terms first so e.g. "philosophy" wins over "philos".
        uniq = sorted(set(folded_terms), key=len, reverse=True)
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(t) for t in uniq) + r")\b"
        )

    import bisect

    snippets: list[dict] = []
    clen = len(content)
    # Build line-offset table once per document (only if we'll need lines).
    line_offsets = _line_offsets(content) if not breaks else None

    for m in pattern.finditer(folded_content):
        if len(snippets) >= max_snippets:
            break
        start = _idx(idx_map, m.start(), clen)
        end_excl = _idx(idx_map, m.end() - 1, clen - 1) + 1
        s = max(0, start - context)
        e = min(clen, end_excl + context)
        text = " ".join(content[s:e].split())
        loc: dict = {"text": text}
        if breaks:
            loc["page"] = _page_of(start, breaks)
        else:
            # bisect_right - 1 gives the index of the line-start ≤ start.
            loc["line"] = bisect.bisect_right(line_offsets, start)
        snippets.append(loc)
    return snippets


def _query_terms(query: str) -> list[str]:
    """Extract the literal terms from a user query, ignoring FTS5 operators.

    Strips: AND, OR, NOT, NEAR, parens, double-quotes (phrase markers).
    """
    import re

    OPS = {"AND", "OR", "NOT", "NEAR"}
    cleaned = re.sub(r'["()]', " ", query)
    parts = re.split(r"\s+", cleaned.strip())
    return [p for p in parts if p and p.upper() not in OPS]


_RAW_OP_RE = re.compile(r"\b(AND|OR|NOT)\b")
_RAW_NEAR_RE = re.compile(r"\bNEAR\s*\(", re.IGNORECASE)


def build_match_expr(query: str, mode: str = "all") -> str:
    """Translate a user query + mode into an FTS5 MATCH expression.

    Modes:
      'all'    → tokens ANDed together (default).
      'phrase' → tokens joined as one contiguous phrase.
      'any'    → tokens ORed together.

    If the raw query already uses FTS5 syntax (double quotes, an uppercase
    AND/OR/NOT operator, or NEAR(...)), it's passed through untouched — power
    users keep full control. Otherwise tokens are extracted via \\w+ (Unicode
    word chars), which also prevents the user from injecting FTS5 syntax
    accidentally.
    """
    if not query or not query.strip():
        return ""

    raw = query.strip()
    if '"' in raw or _RAW_OP_RE.search(raw) or _RAW_NEAR_RE.search(raw):
        return raw

    tokens = re.findall(r"\w+", raw, flags=re.UNICODE)
    if not tokens:
        return ""

    if mode == "phrase":
        return '"' + " ".join(tokens) + '"'
    if mode == "any":
        return " OR ".join(tokens)
    return " AND ".join(tokens)


# --- walk_unindexed (Layer 2 placeholder) -----------------------------------

def walk_unindexed(
    db: sqlite3.Connection,
    folders: Iterable[str | os.PathLike],
    types: Iterable[str],
) -> Iterator[Path]:
    """Yield files under `folders` whose extension is in `types` that are
    not currently indexed (new file, or mtime/size changed).

    Snapshots `files` into memory once so we don't do one SQLite round-trip per
    file (~/Documents trees can be tens of thousands of files).

    Uses os.walk(followlinks=False) so symlinks to directories are never
    recursed into. This prevents infinite loops from self-referential symlinks
    (common in Dropbox, iCloud Drive, and similar sync folders).
    """
    exts = {"." + t.lower().lstrip(".") for t in types}
    # Skip files that are already handled. 'failed' is always retried (tool may
    # now be installed). OCR statuses are managed by walk_ocr_pending separately
    # and must not be re-queued for pdftotext extraction.
    seen: dict[str, tuple[float, int]] = {
        path: (mtime, size)
        for path, mtime, size, status in db.execute(
            "SELECT path, mtime, size, status FROM files"
        )
        if status in ("ok", "unsupported", "ocr_pending", "ocr_running", "ocr_failed")
    }
    for folder in folders:
        root = Path(os.path.expanduser(str(folder)))
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune hidden directories in-place so os.walk never descends into
            # them (e.g. .git, .Trash, hidden sync caches).
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if not fname.startswith(".") and Path(fname).suffix.lower() in exts:
                    p = Path(dirpath) / fname
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    prev = seen.get(str(p))
                    if prev is None:
                        yield p
                        continue
                    mtime, size = prev
                    if mtime != st.st_mtime or size != st.st_size:
                        yield p


def walk_ocr_pending(db: sqlite3.Connection) -> list[Path]:
    """Return paths of scanned PDFs that need OCR.

    Also resets any stale 'ocr_running' entries back to 'ocr_pending' so a
    process crash never permanently loses a file — it will be retried on the
    next search."""
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "UPDATE files SET status='ocr_pending' WHERE status='ocr_running'"
        )
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
    rows = db.execute(
        "SELECT path FROM files WHERE status='ocr_pending'"
    ).fetchall()
    return [Path(r[0]) for r in rows if Path(r[0]).exists()]


def run_ocr_file(db: sqlite3.Connection, path: Path) -> str:
    """OCR a single scanned PDF and write the result to the index.

    Marks the file as 'ocr_running' before starting (crash recovery: the next
    walk_ocr_pending resets 'ocr_running' → 'ocr_pending'). Returns a status
    string: 'ok', 'ocr_failed', or 'error'."""
    # Mark as in-progress so a crash mid-OCR leaves a recoverable state.
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "UPDATE files SET status='ocr_running' WHERE path=?", (str(path),)
        )
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        return "error"

    text, breaks = core.extract_pdf_ocr(path)

    if text and text.strip():
        try:
            st = path.stat()
            write_file(db, path, st.st_mtime, st.st_size, text, breaks)
            return "ok"
        except Exception:
            return "error"
    else:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "UPDATE files SET status='ocr_failed' WHERE path=?", (str(path),)
            )
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
        return "ocr_failed"

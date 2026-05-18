# docsearch — FTS5 index + SSE streaming implementation plan

Living document. Tracks the design and the current stage. Update the **Stage** field and tick boxes as work progresses.

---

## Stage

**Current:** 8 — perf pass + phrase-mode UI complete, 44/44 tests passing.

Stages:
- [x] 0. Plan written.
- [x] 1. Test infrastructure + fixtures.
- [x] 2. Index primitives (Layer 1 — 11 tests).
- [x] 3. Filesystem ↔ index walker (Layer 2 — 6 tests).
- [x] 4. Concurrency (Layer 3 — 3 tests, WAL + busy_timeout=5000ms).
- [x] 5. SSE web layer (Layer 4 — 6 tests).
- [x] 6. Extractor regression tests (Layer 5 — 7 tests).
- [x] 7. CLI bulk-index command + cutover from old `core.search`.
- [x] 8. Perf pass + phrase-mode wiring (see "Perf pass" section below).

---

## Perf pass (2026-05-18)

Benched against the live `~/.cache/docsearch/index.sqlite` (14,805 files, 14,835 indexed docs):

| query                      | mode   | docs | snippets | latency |
|---------------------------|--------|------|----------|---------|
| `philosophy of art`        | phrase | 0    | 0        | **0.3 ms** |
| `art history`              | any    | 200  | 758      | 346 ms  |
| `philosophy art`           | all    | 15   | 225      | 1.56 s  |
| `philosophy`               | all    | 78   | 134      | 1.91 s  |
| `the` (worst-case common)  | all    | 200  | 34k      | 6.8 s   |

### Changes that moved the numbers

- **ASCII fast-path in `_fold`** — skips NFKD normalization on pure-ASCII content, which is most of the corpus. Was per-char, now single `str.lower()`.
- **Bisect for page/line resolution** — `content.count("\n", 0, start)` ran for every match, scanning from offset 0 each time (O(N·M)). Replaced with one precomputed `_line_offsets` table + `bisect.bisect_right` (O(N + M log N)). Same change applied to `_page_of` against `page_breaks`.
- **Per-doc snippet cap (`MAX_SNIPPETS_PER_DOC = 200`)** — UI only shows a handful per file; building 500k snippet dicts for `the` was pure waste.
- **Batched `walk_unindexed` lookup** — `files` table now snapshotted into a dict once instead of one `SELECT … WHERE path = ?` per filesystem entry.
- **Phrase mode** — exposing FTS5 `"…"` phrase queries through a UI dropdown means multi-word queries can stay sub-millisecond. The user-facing "speed" problem is largely "common term across thousands of docs has a lot of matches"; phrase mode sidesteps that by narrowing in SQLite.

### Search query / mode wiring

`docsearch.conf` declared `mode=all|exact|any` but nothing read it. Now:

- `index.build_match_expr(query, mode)` is the single source of truth. Modes: `all` (AND), `phrase` (`"…"`), `any` (OR). Raw FTS5 syntax (`"`, `AND`, `OR`, `NOT`, `NEAR(`) passes through untouched.
- `search()` and `search_one()` take a `mode` kwarg.
- Web UI: form has a mode `<select>`, querystring carries `&mode=…`, both `/?q=` and `/stream?q=` honor it, dropdown round-trips selection.
- CLI: `docsearch search <q> --mode {all,phrase,any}`; CLI also prints indexed hits first, then top-up after extraction (was blocking on full extraction before any output).

### Security hardening

- Already local-only (`127.0.0.1`, no outbound calls, all extractors local).
- Added `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'` and `X-Content-Type-Options: nosniff` to every response.

### Remaining bottleneck (follow-up, not in scope)

`philosophy` single-term query is ~1.9s on a 14k-doc index. Most of that is loading `d.content` for 200 matching docs from SQLite and folding each, regardless of the per-doc snippet cap. Two ways to push further if needed:

1. **Two-phase search**: first `SELECT path, bm25(docs) FROM docs WHERE content MATCH ? ORDER BY bm25 LIMIT N` (no content), then load content only for the top N shown.
2. **FTS5 native `snippet()`**: pull pre-extracted snippets from SQLite for the inline render and only fall back to `_build_snippets` when we need precise page/line for opened results.

Neither is needed for the stated user goals; flag for future work.

---

## Goal

Replace the current "extract every PDF on every query" search with a persistent SQLite FTS5 index. Searches against already-indexed content return instantly. Files not yet indexed are extracted in the background during a search and streamed in via Server-Sent Events. The index grows organically with use.

## Architecture

### Storage

DB path: `~/.cache/docsearch/index.sqlite` (override via `DOCSEARCH_DB` env var).

```sql
CREATE TABLE files (
  path        TEXT PRIMARY KEY,
  mtime       REAL NOT NULL,
  size        INTEGER NOT NULL,
  page_breaks BLOB,              -- packed u32 offsets; NULL for non-paginated
  status      TEXT NOT NULL,     -- 'ok' | 'failed' | 'unsupported'
  error       TEXT,
  indexed_at  REAL NOT NULL
);

CREATE VIRTUAL TABLE docs USING fts5(
  path UNINDEXED,
  content,
  tokenize = 'unicode61 remove_diacritics 2'
);

PRAGMA journal_mode = WAL;
```

`files` is source of truth for "have I seen this?". `docs` holds only successfully extracted content. Failed files live in `files` with `status='failed'` and are not retried unless mtime changes.

### Modules

- `docsearch/index.py` (new) — owns DB. `open_db`, `index_file`, `walk_unindexed`, `search`.
- `docsearch/core.py` — keep extractors. Move/remove old `search()` and `walk_files()`.
- `docsearch/web.py` — split `do_GET`:
  - `GET /?q=…` renders shell + indexed results inline (sync FTS5 query).
  - `GET /stream?q=…` SSE endpoint: walks unindexed files, extracts in a thread pool, persists, streams matches.
- `docsearch/cli.py` — add `docsearch index` for bulk pre-warming.

### SSE flow

```
Browser GET /?q=X
  → server: fts5_search(X) → render inline
  → HTML includes <script>new EventSource('/stream?q=X')</script>

Browser EventSource → GET /stream?q=X
  → server enumerates walk_unindexed(folders, types)
  → event: progress  data: {"total": 423}
  → ThreadPoolExecutor(max_workers=4) submits extract jobs
     → each: extract → match? emit result → persist via writer queue
        event: result    data: {"path": "...", "snippets": [...]}
        event: progress  data: {"done": 17, "total": 423}
  → event: done; close
```

Concurrency model: single writer connection serialized via queue; multiple extractor threads; WAL lets the `/` query thread read concurrently without blocking.

---

## Test infrastructure

- `pyproject.toml` with `pytest` dev dep.
- `tests/fixtures/corpus/` — small hand-built files committed:
  - `philosophy.txt`, `art.md`, `mixed.txt`
  - `cafe.txt` (contains "café")
  - `multipage.txt` (uses `\f` page break markers)
  - `corrupt.pdf` — random bytes, committed because it's just noise, not a fake PDF
- **Real PDF fixture is generated at test time** into `tmp_path_factory` via a session-scoped fixture using `reportlab` (dev dep). Never committed, never present in the source tree visible to the user. If `reportlab` isn't installed the PDF-using test is skipped.
- `tmp_db` fixture: fresh sqlite file per test.

---

## Layers and tests (TDD)

Tick each test as it's written-and-passing.

### Layer 1 — index primitives

- [x] `test_schema_idempotent` — second open doesn't error / duplicate tables.
- [x] `test_index_then_search_txt` — index `philosophy.txt`; search returns path.
- [x] `test_search_empty_index_returns_empty`.
- [x] `test_multiple_matches_one_file` — 5 hits → 5 snippets.
- [x] `test_phrase_vs_and` — "philosophy of art": phrase `"philosophy art"` no match; `philosophy AND art` matches.
- [x] `test_diacritic_insensitive` — index "café", query "cafe" matches.
- [x] `test_case_insensitive` — index "Philosophy", query "philosophy" matches.
- [x] `test_reindex_unchanged_mtime_is_noop` — second `index_file` → `'unchanged'`; FTS row count stable.
- [x] `test_reindex_after_mtime_change_replaces` — old content gone, new content matches, still one row.
- [x] `test_failed_extraction_recorded` — corrupt input → `files.status='failed'`, no `docs` row.
- [x] `test_failed_file_not_retried` — `walk_unindexed` skips failed file with unchanged mtime.
- [x] `test_page_break_roundtrip` — stubbed extractor emits page breaks; hit on page 3 reports `page: 3`.

### Layer 2 — filesystem ↔ index

- [x] `test_walk_unindexed_finds_new_files`.
- [x] `test_walk_unindexed_skips_indexed`.
- [x] `test_walk_unindexed_picks_up_mtime_change`.
- [x] `test_walk_unindexed_respects_type_filter`.
- [x] `test_walk_unindexed_unicode_paths`.

### Layer 3 — concurrency

- [x] `test_concurrent_indexing_two_threads`.
- [x] `test_read_during_write_not_blocked` (WAL).
- [x] `test_writer_retries_on_busy`.

### Layer 4 — SSE / web

- [x] `test_search_endpoint_returns_indexed_results_inline` — pre-populate index; `/?q=…` body contains path.
- [x] `test_stream_emits_done_when_nothing_to_index`.
- [x] `test_stream_indexes_and_emits_result_for_match`.
- [x] `test_stream_persists_index` — after streaming, subsequent inline search returns the file.
- [x] `test_stream_emits_progress_counts`.
- [x] `test_stream_handles_extraction_failure` — corrupt file → stream completes; `files.status='failed'`.

### Layer 5 — extractors (regression net)

- [x] `test_extract_docx_fixture`.
- [x] `test_extract_txt_md`.
- [x] `test_extract_pdf_fixture` — skipif `pdftotext` missing; uses generated tmp PDF.
- [x] `test_extract_corrupt_returns_none` — no exception escapes.

### Explicitly NOT tested

- FTS5 ranking / BM25 quality (SQLite's own test suite).
- pdftotext output fidelity (Poppler's problem).
- HTML/CSS rendering — only endpoint contract.
- Hard perf numbers — out of scope; manual bench script instead.

---

## Decisions log

- **DB location**: `~/.cache/docsearch/index.sqlite`, override `DOCSEARCH_DB`.
- **PDF fixture**: generated at test time into `tmp_path_factory`, never committed.
- **Tokenizer**: `unicode61 remove_diacritics 2` — handles French/German/Latin (humanities corpus).
- **Writer model**: single writer thread + queue; many extractor threads; WAL for read concurrency.
- **Failure handling**: persistent `status='failed'` rows prevent re-extracting broken files every search.
- **Cutover**: old `core.search` removed once Layer 5 is green; no parallel code paths.

## Open / deferred

- File watcher (`fswatch`/`watchdog`) for live updates — out of scope, indexer runs on-demand or via search.
- OCR for scanned PDFs (`ocrmypdf`) — out of scope, separate pre-step.
- Additional formats (`.epub`, `.html`, `.tex`) — easy to add after Layer 5.

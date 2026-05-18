"""Layer 3: concurrent indexing + WAL reader/writer isolation."""
import sqlite3
import threading
import time

import pytest

from docsearch import index


def test_concurrent_indexing_two_threads(tmp_db, corpus):
    """Two writers on separate files should both succeed."""
    index.open_db(tmp_db).close()  # ensure schema exists

    errors: list[BaseException] = []

    def work(filename: str):
        try:
            db = index.open_db(tmp_db)
            assert index.index_file(db, corpus / filename) == "ok"
            db.close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=work, args=("philosophy.txt",))
    t2 = threading.Thread(target=work, args=("art.md",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, f"concurrent indexing raised: {errors}"

    db = index.open_db(tmp_db)
    paths = {row[0] for row in db.execute("SELECT path FROM docs")}
    assert any(p.endswith("philosophy.txt") for p in paths)
    assert any(p.endswith("art.md") for p in paths)


def test_read_during_write_not_blocked(tmp_db, corpus):
    """WAL mode: a reader should not be blocked by an in-progress write."""
    db = index.open_db(tmp_db)
    # Pre-populate so we have something to read.
    index.index_file(db, corpus / "philosophy.txt")

    write_started = threading.Event()
    write_done = threading.Event()

    def slow_write():
        w = index.open_db(tmp_db)
        # Manual transaction so we hold the write lock for a bit.
        w.execute("BEGIN IMMEDIATE")
        write_started.set()
        # Insert a fake row to occupy the write transaction.
        w.execute(
            "INSERT INTO files(path, mtime, size, status, indexed_at) VALUES (?, 0, 0, 'ok', 0)",
            ("/tmp/fake-conc",),
        )
        time.sleep(0.3)
        w.execute("COMMIT")
        write_done.set()
        w.close()

    t = threading.Thread(target=slow_write)
    t.start()
    assert write_started.wait(timeout=2)

    # While the writer is mid-transaction, a reader should still complete fast.
    r = index.open_db(tmp_db)
    t0 = time.monotonic()
    results = index.search(r, "philosophy")
    elapsed = time.monotonic() - t0
    r.close()

    assert results, "reader should see pre-existing committed data"
    assert elapsed < 0.2, f"read was blocked by writer (took {elapsed:.3f}s)"

    t.join()
    assert write_done.is_set()


def test_writer_retries_on_busy(tmp_db, corpus, monkeypatch):
    """If sqlite raises BUSY, index_file should retry rather than fail."""
    index.open_db(tmp_db).close()

    # Force a busy timeout shorter than the artificial lock duration to test
    # the retry-on-busy path. We grab a writer lock from another thread.
    blocker = index.open_db(tmp_db)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO files(path, mtime, size, status, indexed_at) VALUES (?, 0, 0, 'ok', 0)",
        ("/tmp/fake-busy",),
    )

    release = threading.Event()

    def release_after():
        release.wait()
        blocker.execute("COMMIT")
        blocker.close()

    t = threading.Thread(target=release_after)
    t.start()

    result: list = []

    def indexer():
        db = index.open_db(tmp_db)
        try:
            result.append(index.index_file(db, corpus / "art.md"))
        except sqlite3.OperationalError as e:
            result.append(e)
        finally:
            db.close()

    indexer_thread = threading.Thread(target=indexer)
    indexer_thread.start()

    # Let the indexer try briefly while we hold the lock, then release.
    time.sleep(0.2)
    release.set()
    indexer_thread.join(timeout=5)
    t.join(timeout=5)

    assert result == ["ok"], f"expected retry success, got {result}"

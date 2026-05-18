"""CLI entry for docsearch.

Subcommands:
  docsearch <query>          search the index (re-extracts unindexed files inline)
  docsearch index            bulk-index configured folders into the persistent FTS5 index
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import index
from .config import load_config


def _cmd_search(args, cfg):
    folders = args.folders or cfg["folders"]
    types = ([t.strip().lstrip(".") for t in args.types.split(",")]
             if args.types else cfg["types"])
    mode = args.mode or cfg.get("mode") or "all"
    if mode not in ("all", "phrase", "any"):
        sys.stderr.write(f"unknown --mode {mode!r}; falling back to 'all'\n")
        mode = "all"

    db = index.open_db(args.db or index.default_db_path())

    # First: show what's already indexed before doing any extraction work.
    results = index.search(db, args.query, mode=mode)
    _print_results(args.query, results, args.limit, header="indexed hits")

    # Then: pick up anything not yet indexed (mirrors the web /stream path,
    # minus the streaming — CLI just blocks until done).
    pending = list(index.walk_unindexed(db, folders, types))
    if pending:
        sys.stderr.write(f"indexing {len(pending)} new files…\n")
        for p in pending:
            try:
                index.index_file(db, p)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"  skipping {p}: {e}\n")
        # Re-query to include freshly indexed hits, then print only the delta.
        prev_paths = {p for p, _ in results}
        new_results = [r for r in index.search(db, args.query, mode=mode)
                       if r[0] not in prev_paths]
        if new_results:
            _print_results(args.query, new_results, args.limit,
                           header="newly indexed hits")
        results = results + new_results

    if not results:
        print("No matches.")
    return


def _print_results(query, results, limit, header=None):
    if not results:
        return
    if header:
        print(f"\n— {header} —")
    print(f"\nQuery: {query!r}   files matched: {len(results)}\n")
    for path, snippets in results:
        n = len(snippets)
        print(f"📄 {path.name}  ({n} match{'es' if n != 1 else ''})")
        print(f"   {path}")
        shown = snippets if limit == 0 else snippets[:limit]
        for loc in shown:
            tag = f"p.{loc['page']}" if "page" in loc else f"L{loc.get('line', '?')}"
            print(f"   {tag}: …{loc['text']}…")
        if limit and n > limit:
            print(f"   … {n - limit} more")
        print()


def _cmd_index(args, cfg):
    folders = args.folders or cfg["folders"]
    types = ([t.strip().lstrip(".") for t in args.types.split(",")]
             if args.types else cfg["types"])

    db_path = args.db or index.default_db_path()
    db = index.open_db(db_path)

    pending = list(index.walk_unindexed(db, folders, types))
    total = len(pending)
    if total == 0:
        print(f"Nothing to index. Index up to date at {db_path}")
        return

    print(f"Indexing {total} files into {db_path}")
    t0 = time.monotonic()
    done = 0

    def work(path):
        wdb = index.open_db(db_path)
        try:
            return path, index.index_file(wdb, path)
        finally:
            wdb.close()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed(ex.submit(work, p) for p in pending):
            done += 1
            try:
                path, status = fut.result()
            except Exception as exc:
                sys.stderr.write(f"\r  {done}/{total} (error      ) {exc!s:.60}\033[K\n")
                sys.stderr.flush()
                continue
            if done % 25 == 0 or done == total:
                sys.stderr.write(f"\r  {done}/{total} ({status:11s}) {path.name[:60]}\033[K")
                sys.stderr.flush()

    elapsed = time.monotonic() - t0
    print(f"\nIndexed {total} files in {elapsed:.1f}s.")


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(
        prog="docsearch",
        description="Local full-text search across documents (docx, pdf, doc, rtf, pages, txt, md).",
    )
    ap.add_argument("--db", help=f"Index DB path (default: {index.default_db_path()})")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("index", help="Bulk-index configured folders.")
    sp.add_argument("--in", dest="folders", action="append", metavar="DIR")
    sp.add_argument("--types", help="Comma-separated extensions")
    sp.add_argument("--workers", type=int, default=4)

    # Default subcommand is `search`, which takes a positional query.
    sq = sub.add_parser("search", help="Search the index.")
    sq.add_argument("query")
    sq.add_argument("--in", dest="folders", action="append", metavar="DIR")
    sq.add_argument("--types", help="Comma-separated extensions")
    sq.add_argument("--limit", type=int, default=5)
    sq.add_argument(
        "--mode",
        choices=("all", "phrase", "any"),
        default=None,
        help="Match mode: all words (AND), exact phrase, or any word (OR). "
             "Defaults to config 'mode' (or 'all').",
    )

    # Convenience: `docsearch <query>` with no subcommand → treat as search.
    # We have to inject "search" before argparse, because subparsers will
    # otherwise reject an unknown first positional.
    argv = sys.argv[1:]
    known_cmds = {"index", "search"}
    # Find the first non-option token; if it isn't a known subcommand, splice
    # "search" in front of it. (Options like --db, --in are passed through.)
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        # Options that consume a value
        if argv[i] in ("--db", "--in", "--types", "--limit", "--workers", "--mode"):
            i += 2
        else:
            i += 1
    if i >= len(argv):
        ap.print_help()
        sys.exit(1)
    if argv[i] not in known_cmds:
        argv = argv[:i] + ["search"] + argv[i:]
    args = ap.parse_args(argv)

    if args.cmd == "index":
        _cmd_index(args, cfg)
    else:
        _cmd_search(args, cfg)


if __name__ == "__main__":
    main()

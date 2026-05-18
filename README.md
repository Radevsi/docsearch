# docsearch

Local full-text search across your documents (`docx`, `pdf`, `doc`, `rtf`, `pages`, `txt`, `md`). Backed by SQLite FTS5. Nothing ever leaves your machine.

- **Fast**: persistent index; cold queries against indexed content return in milliseconds.
- **Local-only**: the web UI binds to a random `127.0.0.1` port and is gated by a 256-bit session token written to a `0600` file. No outbound network calls, anywhere.
- **Useful**: case- and diacritic-insensitive search; phrase / all-words / any-word modes; page-number and line-number locations in results.

## Requirements

- macOS (the `textutil` extractor for `doc`/`rtf`/`pages` is Apple-only; everything else is portable).
- Python 3.9+
- For PDF search: `brew install poppler` (provides `pdftotext`). Skip this if you don't need PDFs.

## Install

```sh
git clone <your-fork-url> docsearch
cd docsearch
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

### Web UI

```sh
docsearch-web
```

This prints a launch URL like `http://127.0.0.1:53412/?token=…` and opens it in your default browser. The token is also stored at `~/.cache/docsearch/auth-token` (mode 0600) — keep it private.

In the UI, the **mode** dropdown next to the search box switches between:

- **all words** — every term must appear (default).
- **exact phrase** — the words must appear contiguous, in order.
- **any word** — at least one term matches.

You can also type FTS5 syntax directly: `"foo bar"`, `foo AND bar`, `NEAR(foo bar, 5)`, etc.

### Command line

```sh
docsearch "philosophy of art"                       # all-words AND
docsearch "philosophy of art" --mode phrase         # exact phrase
docsearch "art history" --mode any                  # OR
docsearch index                                     # bulk pre-warm the index
```

### Configure folders / file types

Copy `docsearch.conf` and edit:

```
folder=~/Documents
folder=~/Downloads
folder=~/Desktop

type=docx
type=pdf
type=txt
type=md

mode=all
context=80
```

The shipped defaults are reasonable; override per-call with `--in` / `--types`.

## Security model

- The web server only binds to `127.0.0.1`. It is unreachable from outside your machine.
- Every request must carry a 256-bit session token. Without it, the server returns `401`.
- Cross-origin requests are rejected (`403`), so a malicious web page in your browser can't make the server `open` a file or read your index.
- The token file `~/.cache/docsearch/auth-token` is created `0600`. Don't share it.

## Run the tests

```sh
pip install -e '.[dev]'
pytest
```

## Configuration / data locations

- Index: `~/.cache/docsearch/index.sqlite` (override via `DOCSEARCH_DB`).
- Session token: `~/.cache/docsearch/auth-token` (override base dir via `DOCSEARCH_CACHE_DIR`).
- Port: ephemeral by default; override via `DOCSEARCH_PORT=8765`.

## License

MIT — see `LICENSE`.

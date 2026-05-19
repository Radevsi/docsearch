# docsearch

Search across all your documents instantly — `docx`, `pdf`, `doc`, `rtf`, `pages`, `txt`, `md`. Everything runs on your Mac. Nothing is uploaded anywhere.

- **Fast** — results appear in milliseconds once files are indexed.
- **Private** — runs entirely on your machine, no internet connection required.
- **Persistent** — the index is saved between sessions; already-indexed files are instant.

---

## Getting started

### Step 1 — Download

Open **Terminal** and run:

```sh
git clone https://github.com/Radevsi/docsearch.git
```

This puts a `docsearch` folder on your computer. Move it wherever you like (e.g. your home folder or Desktop).

> **Don't have git?** Download it from [git-scm.com](https://git-scm.com/download/mac) — it's free and takes about a minute.

### Step 2 — Install Python (if you don't have it)

Open **Terminal** and run:

```sh
python3 --version
```

If you see `Python 3.9` or higher you're good. Otherwise, download Python from [python.org/downloads](https://www.python.org/downloads/) and run the installer.

### Step 3 — Launch

Open the `docsearch` folder in Finder and double-click **Launch docsearch.command**.

- The first time: Terminal will open, set things up automatically (takes about 30 seconds), and then open docsearch in your browser.
- Every time after: it checks for updates automatically — if there's a newer version it installs it, then opens in a few seconds.

> **"This cannot be opened"?** Right-click the file → **Open** → **Open** again. You'll only need to do this once.

---

## Optional add-ons

All of these require [Homebrew](https://brew.sh). Install it first if you don't have it.

### PDF support (text-based PDFs)

```sh
brew install poppler
```

Required to search inside PDFs. Without it, PDF files are skipped entirely.

### Scanned PDF support (OCR)

```sh
brew install ocrmypdf
```

Enables searching inside scanned PDFs (e.g. photocopied documents). Without it, scanned PDFs are detected but not indexed. OCR runs automatically in the background — re-search after a few minutes to see results.

### Greek language OCR

```sh
brew install tesseract-lang
```

Required if you want to OCR scanned PDFs that contain Greek text. Without it, OCR defaults to English only and will misread Greek characters. Install this alongside `ocrmypdf`.

---

## How it works

- **Searching** — type anything in the search box and press Enter. The first search after a fresh install is slower as files get indexed; all subsequent searches are instant.
- **File types** — use the dropdown next to the search box to filter by file type (e.g. PDFs only).
- **Folders** — click **Folders…** to pick a specific folder to search within.
- **Match mode** — switch between *all words*, *exact phrase*, and *any word*.
- **Opening a result** — click the filename in any result to open the file directly.

Your index is saved at `~/.cache/docsearch/index.sqlite` and persists across restarts. The version and index location are shown at the bottom of the page.

---

## Updates

**Nothing to do.** Every time you launch, the launcher checks GitHub for new versions and installs them automatically before opening the app. As long as you cloned via `git clone` (Step 1), you'll always be on the latest version.

---

## Security

- The search UI is only accessible from your own machine (`127.0.0.1`).
- Every session is protected by a token that is never stored in your browser history.
- No data ever leaves your computer.

---

## For developers

```sh
# Run tests
pip install -e '.[dev]'
pytest

# Configure folders and file types
# Edit docsearch.conf in the project root

# Data locations
# Index:       ~/.cache/docsearch/index.sqlite  (override: DOCSEARCH_DB)
# Auth token:  ~/.cache/docsearch/auth-token    (override: DOCSEARCH_CACHE_DIR)
# Port:        ephemeral by default             (override: DOCSEARCH_PORT=8765)
```

## License

MIT — see `LICENSE`.

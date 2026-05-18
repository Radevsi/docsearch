# docsearch

Search across all your documents instantly — `docx`, `pdf`, `doc`, `rtf`, `pages`, `txt`, `md`. Everything runs on your Mac. Nothing is uploaded anywhere.

- **Fast** — results appear in milliseconds once files are indexed.
- **Private** — runs entirely on your machine, no internet connection required.
- **Persistent** — the index is saved between sessions; already-indexed files are instant.

---

## Getting started

### Step 1 — Download

Click the green **Code** button on this page, choose **Download ZIP**, unzip it, and move the `docsearch` folder wherever you like (e.g. your home folder or Desktop).

Or, if you use git:

```sh
git clone https://github.com/Radevsi/docsearch.git
```

### Step 2 — Install Python (if you don't have it)

Open **Terminal** and run:

```sh
python3 --version
```

If you see `Python 3.9` or higher you're good. Otherwise, download Python from [python.org/downloads](https://www.python.org/downloads/) and run the installer.

### Step 3 — Launch

Open the `docsearch` folder in Finder and double-click **Launch docsearch.command**.

- The first time: Terminal will open, set things up automatically (takes about 30 seconds), and then open docsearch in your browser.
- Every time after: it opens in a few seconds.

> **"This cannot be opened"?** Right-click the file → **Open** → **Open** again. You'll only need to do this once.

---

## Optional: PDF support

PDFs are supported but require a free tool called Poppler. If you want to search inside PDFs, install [Homebrew](https://brew.sh) first, then run in Terminal:

```sh
brew install poppler
```

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

When an update is available, you'll see a notice in the Terminal window when you launch. To update:

```sh
cd docsearch
git pull
pip install -e .
```

Then relaunch as normal.

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

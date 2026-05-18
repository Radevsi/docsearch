"""Config loading for docsearch."""
from pathlib import Path

DEFAULTS = {
    "folders": ["~/Documents"],
    "types": ["docx", "pdf", "doc", "rtf", "pages", "txt", "md"],
    "mode": "all",
    "context": 80,
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATHS = [
    Path.home() / ".config" / "docsearch" / "config",
    PROJECT_ROOT / "docsearch.conf",
    Path.cwd() / "docsearch.conf",
]


def load_config():
    cfg = {"folders": [], "types": [], "mode": None, "context": None}
    for path in CONFIG_PATHS:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = (s.strip() for s in line.split("=", 1))
            if k == "folder":
                cfg["folders"].append(v)
            elif k == "type":
                cfg["types"].append(v.lstrip("."))
            elif k == "mode":
                cfg["mode"] = v
            elif k == "context":
                cfg["context"] = int(v)
        break
    for k, v in DEFAULTS.items():
        if not cfg.get(k):
            cfg[k] = v
    return cfg

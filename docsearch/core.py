"""Extraction primitives. Search lives in `index.py`."""
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def extract_docx(path):
    ns_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    ns_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    try:
        with zipfile.ZipFile(path) as z, z.open("word/document.xml") as f:
            paras: list[str] = []
            current: list[str] = []
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag == ns_t:
                    current.append(elem.text or "")
                    elem.clear()
                elif elem.tag == ns_p:
                    paras.append("".join(current))
                    current = []
                    elem.clear()
    except Exception:
        return None, None
    return "\n".join(paras), None


_pdftotext_warned = False


def extract_pdf(path):
    global _pdftotext_warned
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=90,
        )
    except FileNotFoundError:
        if not _pdftotext_warned:
            _pdftotext_warned = True
            sys.stderr.write(
                "warning: pdftotext not found — PDFs will be skipped. "
                "Install with: brew install poppler\n"
            )
        return None, None
    except subprocess.TimeoutExpired:
        return None, None
    if r.returncode != 0:
        return None, None
    text = r.stdout
    breaks = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\f"]
    return text, breaks


def extract_textutil(path):
    try:
        r = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    if r.returncode != 0:
        return None, None
    return r.stdout, None


def extract_plain(path):
    try:
        return Path(path).read_text(errors="replace"), None
    except Exception:
        return None, None


EXTRACTORS = {
    "docx": extract_docx,
    "pdf": extract_pdf,
    "doc": extract_textutil,
    "rtf": extract_textutil,
    "pages": extract_textutil,
    "txt": extract_plain,
    "md": extract_plain,
}


def extract(path):
    """Return (text, page_breaks) for path, or (None, None) if unsupported/failed."""
    ext = Path(path).suffix.lower().lstrip(".")
    fn = EXTRACTORS.get(ext)
    if not fn:
        return None, None
    return fn(path)

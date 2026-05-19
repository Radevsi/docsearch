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
_ocr_warned = False

# Minimum non-whitespace characters returned by pdftotext before we decide the
# PDF has real embedded text. Below this threshold we assume it's scanned.
OCR_TEXT_THRESHOLD = 50


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


def extract_pdf_ocr(path):
    """OCR fallback for scanned PDFs using ocrmypdf + pdftotext.

    ocrmypdf runs Tesseract on each page image and writes a searchable PDF to
    a temp file; pdftotext then extracts the text. Both tools must be installed:
      brew install ocrmypdf   (pulls in tesseract and ghostscript)
      brew install poppler    (for pdftotext — likely already present)

    Returns (text, page_breaks) or (None, None) if OCR fails or tool is absent.
    Deliberately has a long timeout (10 min) — scanned PDFs are slow."""
    global _ocr_warned
    import os as _os
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    _os.close(tmp_fd)
    try:
        try:
            r = subprocess.run(
                ["ocrmypdf", "--force-ocr", "--jobs", "1", "--quiet",
                 str(path), tmp_path],
                capture_output=True, timeout=600,
            )
        except FileNotFoundError:
            if not _ocr_warned:
                _ocr_warned = True
                sys.stderr.write(
                    "warning: ocrmypdf not found — scanned PDFs cannot be OCR'd. "
                    "Install with: brew install ocrmypdf\n"
                )
            return None, None
        except subprocess.TimeoutExpired:
            return None, None
        if r.returncode != 0:
            return None, None
        r2 = subprocess.run(
            ["pdftotext", "-layout", tmp_path, "-"],
            capture_output=True, text=True, timeout=90,
        )
        if r2.returncode != 0 or not r2.stdout.strip():
            return None, None
        text = r2.stdout
        breaks = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\f"]
        return text, breaks
    except Exception:
        return None, None
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


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

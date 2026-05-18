"""Layer 5: extractor regression tests."""
import shutil
import zipfile
from pathlib import Path

import pytest

from docsearch import core


def _make_docx(path: Path, paragraphs: list[str]) -> None:
    """Build a minimal but valid .docx with the given paragraphs."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    document_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document_xml)


def test_extract_docx(tmp_path):
    p = tmp_path / "sample.docx"
    _make_docx(p, ["Hello philosophy.", "A second paragraph about art."])
    text, breaks = core.extract(p)
    assert text is not None
    assert "philosophy" in text
    assert "art" in text
    assert breaks is None


def test_extract_txt(corpus):
    text, breaks = core.extract(corpus / "philosophy.txt")
    assert text is not None
    assert "philosophy" in text.lower()
    assert breaks is None


def test_extract_md(corpus):
    text, breaks = core.extract(corpus / "art.md")
    assert text is not None
    assert "art" in text.lower()


def test_extract_corrupt_returns_none(corpus):
    """A garbage .pdf must not raise — extractor returns (None, None)."""
    text, breaks = core.extract(corpus / "corrupt.pdf")
    assert text is None
    assert breaks is None


def test_extract_unsupported_extension(tmp_path):
    p = tmp_path / "weird.xyz"
    p.write_text("hello")
    text, breaks = core.extract(p)
    assert text is None
    assert breaks is None


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
def test_extract_pdf(generated_pdf):
    text, breaks = core.extract(generated_pdf)
    assert text is not None
    assert "alpha" in text
    assert "beta" in text
    assert "gamma" in text
    # 3-page PDF → page_breaks should include start of each page.
    assert breaks is not None
    assert len(breaks) >= 3


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
def test_extract_pdf_pages_correct(generated_pdf):
    """Search 'beta' in the indexed PDF should report page 2."""
    from docsearch import index

    db = index.open_db(":memory:" if False else (generated_pdf.parent / "idx.sqlite"))
    assert index.index_file(db, generated_pdf) == "ok"
    results = index.search(db, "beta")
    assert results
    _, snippets = results[0]
    assert snippets[0]["page"] == 2

    results = index.search(db, "gamma")
    _, snippets = results[0]
    assert snippets[0]["page"] == 3

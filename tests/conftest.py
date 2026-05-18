import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "index.sqlite"


@pytest.fixture
def corpus(tmp_path):
    """Copy the committed fixture corpus into tmp_path/corpus and return its path.

    Tests get a writable, isolated copy so they can mutate mtimes / add files
    without contaminating other tests.
    """
    dst = tmp_path / "corpus"
    shutil.copytree(FIXTURES, dst)
    return dst


@pytest.fixture(scope="session")
def generated_pdf(tmp_path_factory):
    """A real 3-page PDF generated at test time. Never committed.

    Page 1 mentions 'alpha', page 2 mentions 'beta', page 3 mentions 'gamma'.
    Skipped if reportlab isn't installed.
    """
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    path = tmp_path_factory.mktemp("pdf") / "generated.pdf"
    c = canvas.Canvas(str(path))
    for page_word in ("alpha introduction", "beta middle", "gamma conclusion"):
        c.drawString(100, 750, f"This page is about {page_word}.")
        c.showPage()
    c.save()
    return path

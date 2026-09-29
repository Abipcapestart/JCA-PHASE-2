import pytest

import methodology


def test_load_methodology_against_real_pdf():
    ctx = methodology.load_methodology(force_reload=True)
    assert ctx.methodology_loaded is True
    assert len(ctx.sections_covered) == 4
    assert "3.2.1 Step 1: List the requirements per MS" in ctx.sections_covered
    assert "unique" in ctx.rule_text.lower() or "UNIQUE" in ctx.rule_text


def test_missing_pdf_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(methodology, "HTA_GUIDANCE_PDF_PATH", str(tmp_path / "does_not_exist.pdf"))
    methodology._cache = None
    with pytest.raises(FileNotFoundError):
        methodology.load_methodology(force_reload=True)
    methodology._cache = None  # don't leak a bad monkeypatched state into other tests

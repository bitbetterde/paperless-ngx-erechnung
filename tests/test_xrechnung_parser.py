# SPDX-License-Identifier: GPL-3.0-only
"""End-to-end tests for XRechnungParser.

The full ``parse()`` path renders an archive PDF via the vendored KoSIT
XSLT and WeasyPrint. Those are heavy deps; tests that exercise them are
gated on ``saxonche`` and ``weasyprint`` being importable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_ngx_erechnung.parsers import XRechnungParser


# --------------------------------------------------------------------------- #
# Registry contract (no parse() needed)
# --------------------------------------------------------------------------- #


def test_required_class_attributes_present() -> None:
    for attr in ("name", "version", "author", "url"):
        value = getattr(XRechnungParser, attr)
        assert isinstance(value, str) and value


def test_supported_mime_types_covers_xml() -> None:
    mimes = XRechnungParser.supported_mime_types()
    assert "application/xml" in mimes
    assert "text/xml" in mimes


def test_score_returns_none_for_unsupported_mime() -> None:
    assert XRechnungParser.score("application/pdf", "x.pdf") is None


def test_score_advertises_capability_without_path() -> None:
    # paperless-ngx's is_mime_type_supported() calls score() with path=None
    # to decide whether the upload endpoint should accept the file. We must
    # answer non-None for our MIME types, or every text/xml upload is
    # rejected with "File type text/xml not supported".
    assert XRechnungParser.score("application/xml", "x.xml") is not None


def test_score_returns_none_for_non_xrechnung_xml(tmp_path: Path) -> None:
    f = tmp_path / "boring.xml"
    f.write_text("<root/>")
    # Sniff doesn't see a UBL/CII namespace marker → score declines without
    # claiming, so an unrelated XML file falls through to paperless's
    # "no parser" path rather than producing one of our error messages.
    assert XRechnungParser.score("application/xml", "boring.xml", f) is None


# Real-world malformed pattern: the CII root namespace is intact (so the
# sniff sees the marker), but the ram: namespace URI was hard-wrapped at
# column 80 by a buggy producer — XML normalises the embedded LF to a
# space, leaving an invalid URI. Reproduces the bug we saw on a customer file.
_BROKEN_RAM_NS_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b"<rsm:CrossIndustryInvoice\n"
    b'xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"\n'
    b'xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformat\n'
    b'ionEntity:100"/>'
)


def test_score_claims_files_with_invoice_namespace_even_when_invalid(
    tmp_path: Path,
) -> None:
    """Sniff is loose so parse() can raise specific error messages.

    A file mentioning the CII namespace but otherwise malformed must be
    *claimed* — paperless will then call parse() and surface our specific
    ErechnungValidationError instead of "Unsupported mime type".
    """
    f = tmp_path / "broken.xml"
    f.write_bytes(_BROKEN_RAM_NS_XML)
    score = XRechnungParser.score("application/xml", "broken.xml", f)
    assert score is not None and score > 10


def test_parse_raises_parse_error_with_line_column_for_malformed_xml(
    tmp_path: Path,
) -> None:
    from documents.parsers import ParseError  # noqa: PLC0415

    f = tmp_path / "broken.xml"
    f.write_bytes(_BROKEN_RAM_NS_XML)
    with XRechnungParser() as parser, pytest.raises(ParseError) as excinfo:
        parser.parse(f, "application/xml", produce_archive=False)
    msg = str(excinfo.value)
    assert "XML parse error" in msg
    assert "line " in msg and "column " in msg


def test_parse_raises_parse_error_for_generic_en16931(tmp_path: Path) -> None:
    from documents.parsers import ParseError  # noqa: PLC0415

    f = tmp_path / "generic.xml"
    f.write_text(
        """<?xml version="1.0"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
</Invoice>
""",
    )
    with XRechnungParser() as parser, pytest.raises(ParseError) as excinfo:
        parser.parse(f, "application/xml", produce_archive=False)
    msg = str(excinfo.value)
    assert "urn:cen.eu:en16931:2017" in msg
    assert "xrechnung" in msg.lower()


def test_score_wins_for_xrechnung(ubl_invoice_path: Path) -> None:
    score = XRechnungParser.score(
        "application/xml",
        "invoice.xml",
        ubl_invoice_path,
    )
    # Built-in parsers score 10; we must outrank them.
    assert score is not None and score > 10


def test_properties() -> None:
    parser = XRechnungParser()
    assert parser.can_produce_archive is True
    assert parser.requires_pdf_rendition is True


def test_context_manager_cleans_up_tempdir() -> None:
    with XRechnungParser() as p:
        tempdir = p._tempdir  # noqa: SLF001
        assert tempdir.is_dir()
    assert not tempdir.is_dir()


# --------------------------------------------------------------------------- #
# extract_metadata (no archive PDF needed — pure parse path)
# --------------------------------------------------------------------------- #


def test_extract_metadata_from_disk(ubl_invoice_path: Path) -> None:
    with XRechnungParser() as parser:
        entries = parser.extract_metadata(ubl_invoice_path, "application/xml")

    keys = {e["key"] for e in entries}
    assert "invoice_number" in keys
    assert "issue_date" in keys
    assert "total_amount" in keys
    # All entries share our namespace.
    assert all(
        e["namespace"] == "urn:paperless-ngx-erechnung:xrechnung" for e in entries
    )
    assert all(e["prefix"] == "erechnung" for e in entries)


# --------------------------------------------------------------------------- #
# Full parse() — heavy: saxonche + KoSIT XSLT + WeasyPrint
# --------------------------------------------------------------------------- #


def _rendering_available() -> tuple[bool, str]:
    """Return (available, reason) for the heavy rendering stack."""
    try:
        import saxonche  # noqa: F401, PLC0415
    except ImportError as exc:
        return False, f"saxonche unavailable: {exc}"
    try:
        import weasyprint  # noqa: F401, PLC0415
    except (ImportError, OSError) as exc:
        # WeasyPrint also fails to import (OSError) when its native deps
        # (pango, cairo, gdk-pixbuf) are missing from the system.
        return False, f"weasyprint unavailable: {exc}"
    return True, ""


_RENDER_OK, _RENDER_REASON = _rendering_available()
needs_rendering = pytest.mark.skipif(not _RENDER_OK, reason=_RENDER_REASON)


@needs_rendering
def test_full_parse_produces_archive_and_text(ubl_invoice_path: Path) -> None:
    with XRechnungParser() as parser:
        parser.parse(ubl_invoice_path, "application/xml")

        archive = parser.get_archive_path()
        text = parser.get_text()
        date = parser.get_date()

        assert archive is not None and archive.is_file()
        assert archive.stat().st_size > 0
        assert text is not None
        # Key-value block must be present at the top of the searchable text.
        assert "Invoice-Number: 1234567890" in text
        assert "Currency: EUR" in text
        assert date is not None and date.year == 2018


@needs_rendering
def test_rendered_pdf_has_no_javascript_warning_or_tab_buttons(
    ubl_invoice_path: Path,
) -> None:
    """Print-mode CSS must suppress the KoSIT viewer chrome.

    Without the print stylesheet, the archive PDF contains:
      - the German "JavaScript required" notice from <noscript>,
      - dead tab buttons (Übersicht / Details / Zusätze / Anlagen / Laufzettel),
    because WeasyPrint can't execute the viewer's JavaScript.
    """
    import pypdfium2 as pdfium  # noqa: PLC0415

    with XRechnungParser() as parser:
        parser.parse(ubl_invoice_path, "application/xml")
        archive = parser.get_archive_path()
        assert archive is not None and archive.is_file()

        # Extract text from every page and concatenate.
        doc = pdfium.PdfDocument(str(archive))
        try:
            page_text = "\n".join(
                (page.get_textpage().get_text_range() or "") for page in doc
            )
        finally:
            doc.close()

    lowered = page_text.lower()
    assert "javascript" not in lowered, (
        "PDF still contains the <noscript> 'JavaScript required' notice."
    )
    # The four normally-hidden tabs are unfolded for print, so their content
    # is in the PDF — but the *tab buttons themselves* should not be.
    # The button-only labels are short German words; we test the rarest one.
    assert "Laufzettel" not in page_text, (
        "PDF still contains a viewer tab button label (Laufzettel)."
    )


@needs_rendering
def test_parse_without_archive_skips_pdf(ubl_invoice_path: Path) -> None:
    with XRechnungParser() as parser:
        parser.parse(
            ubl_invoice_path,
            "application/xml",
            produce_archive=False,
        )
        assert parser.get_archive_path() is None
        # We still get the searchable key-value block.
        text = parser.get_text()
        assert text is not None and "Invoice-Number" in text


@needs_rendering
def test_thumbnail_after_parse(ubl_invoice_path: Path) -> None:
    with XRechnungParser() as parser:
        parser.parse(ubl_invoice_path, "application/xml")
        thumb = parser.get_thumbnail(ubl_invoice_path, "application/xml")

        assert thumb.is_file()
        assert thumb.read_bytes()[:4] == b"RIFF"  # WebP magic

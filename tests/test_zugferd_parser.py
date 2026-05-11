# SPDX-License-Identifier: GPL-3.0-only
"""End-to-end tests for ZUGFeRDParser."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_ngx_erechnung.parsers import ZUGFeRDParser


# A real ZUGFeRD PDF is heavy to vendor; we build minimal hybrids at runtime.
pytest.importorskip("pikepdf")


@pytest.fixture
def zugferd_pdf(tmp_path: Path, cii_invoice_bytes: bytes) -> Path:
    """Build a minimal Factur-X-style hybrid PDF for tests."""
    import pikepdf  # noqa: PLC0415

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    attachment = pikepdf.AttachedFileSpec(
        pdf,
        cii_invoice_bytes,
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments["factur-x.xml"] = attachment

    out = tmp_path / "zugferd.pdf"
    pdf.save(str(out))
    return out


@pytest.fixture
def plain_pdf(tmp_path: Path) -> Path:
    import pikepdf  # noqa: PLC0415

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    out = tmp_path / "plain.pdf"
    pdf.save(str(out))
    return out


# --------------------------------------------------------------------------- #
# Registry contract
# --------------------------------------------------------------------------- #


def test_required_class_attributes_present() -> None:
    for attr in ("name", "version", "author", "url"):
        value = getattr(ZUGFeRDParser, attr)
        assert isinstance(value, str) and value


def test_supported_mime_types_is_pdf_only() -> None:
    assert ZUGFeRDParser.supported_mime_types() == {"application/pdf": ".pdf"}


def test_score_none_for_non_pdf_mime() -> None:
    assert ZUGFeRDParser.score("application/xml", "x.xml") is None


def test_score_none_for_plain_pdf(plain_pdf: Path) -> None:
    """ZUGFeRDParser must decline plain PDFs so the built-in path wins."""
    assert ZUGFeRDParser.score("application/pdf", "plain.pdf", plain_pdf) is None


def test_score_wins_for_zugferd_pdf(zugferd_pdf: Path) -> None:
    score = ZUGFeRDParser.score("application/pdf", "zugferd.pdf", zugferd_pdf)
    assert score is not None and score > 10


def test_properties() -> None:
    parser = ZUGFeRDParser()
    assert parser.can_produce_archive is True
    # Source already is a PDF the browser can render.
    assert parser.requires_pdf_rendition is False


# --------------------------------------------------------------------------- #
# Full parse() — needs only pikepdf + pypdfium2
# --------------------------------------------------------------------------- #


def test_parse_extracts_metadata_and_archive(zugferd_pdf: Path) -> None:
    with ZUGFeRDParser() as parser:
        parser.parse(zugferd_pdf, "application/pdf")

        archive = parser.get_archive_path()
        text = parser.get_text()
        date = parser.get_date()

        assert archive is not None and archive.is_file()
        # Archive is a byte-identical copy of the input.
        assert archive.read_bytes() == zugferd_pdf.read_bytes()

        assert text is not None
        assert "Invoice-Number: RR123456" in text
        assert "Currency: EUR" in text
        assert date is not None and date.year == 2016


def test_parse_without_archive_skips_copy(zugferd_pdf: Path) -> None:
    with ZUGFeRDParser() as parser:
        parser.parse(zugferd_pdf, "application/pdf", produce_archive=False)
        assert parser.get_archive_path() is None
        # The key-value block is still produced from the embedded XML.
        text = parser.get_text()
        assert text is not None and "Invoice-Number" in text


def test_extract_metadata(zugferd_pdf: Path) -> None:
    with ZUGFeRDParser() as parser:
        entries = parser.extract_metadata(zugferd_pdf, "application/pdf")

    keys = {e["key"] for e in entries}
    assert "invoice_number" in keys
    assert all(e["namespace"] == "urn:paperless-ngx-erechnung:zugferd" for e in entries)


def test_page_count(zugferd_pdf: Path) -> None:
    with ZUGFeRDParser() as parser:
        assert parser.get_page_count(zugferd_pdf, "application/pdf") == 1


def test_thumbnail(zugferd_pdf: Path) -> None:
    with ZUGFeRDParser() as parser:
        thumb = parser.get_thumbnail(zugferd_pdf, "application/pdf")
        assert thumb.is_file()
        assert thumb.read_bytes()[:4] == b"RIFF"


def test_parse_raises_for_missing_file(tmp_path: Path) -> None:
    from documents.parsers import ParseError  # provided by the test shim

    with ZUGFeRDParser() as parser, pytest.raises(ParseError):
        parser.parse(tmp_path / "missing.pdf", "application/pdf")


# --------------------------------------------------------------------------- #
# parse() error-message contract — every rejection path tells the user why.
# --------------------------------------------------------------------------- #


def test_score_claims_pdf_with_broken_attachment(tmp_path: Path) -> None:
    """Sniff matches on the filename alone so parse() can produce a real error."""
    import pikepdf  # noqa: PLC0415

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.attachments["factur-x.xml"] = pikepdf.AttachedFileSpec(
        pdf,
        b"<root/>",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    out = tmp_path / "stub.pdf"
    pdf.save(str(out))
    score = ZUGFeRDParser.score("application/pdf", "stub.pdf", out)
    assert score is not None and score > 10


def test_parse_raises_for_stub_attachment(tmp_path: Path) -> None:
    from documents.parsers import ParseError  # provided by the test shim
    import pikepdf  # noqa: PLC0415

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.attachments["factur-x.xml"] = pikepdf.AttachedFileSpec(
        pdf,
        b"<root/>",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    out = tmp_path / "stub.pdf"
    pdf.save(str(out))

    with ZUGFeRDParser() as parser, pytest.raises(ParseError) as excinfo:
        parser.parse(out, "application/pdf", produce_archive=False)
    msg = str(excinfo.value)
    assert "factur-x.xml" in msg
    assert "Unexpected XML root" in msg


def test_parse_raises_for_generic_en16931_attachment(tmp_path: Path) -> None:
    from documents.parsers import ParseError  # provided by the test shim
    import pikepdf  # noqa: PLC0415

    generic_cii = (
        b'<?xml version="1.0"?>\n'
        b"<rsm:CrossIndustryInvoice\n"
        b'xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"\n'
        b'xmlns:ram="urn:un:unece:uncefact:data:standard:'
        b'ReusableAggregateBusinessInformationEntity:100">'
        b"<rsm:ExchangedDocumentContext>"
        b"<ram:GuidelineSpecifiedDocumentContextParameter>"
        b"<ram:ID>urn:cen.eu:en16931:2017</ram:ID>"
        b"</ram:GuidelineSpecifiedDocumentContextParameter>"
        b"</rsm:ExchangedDocumentContext>"
        b"</rsm:CrossIndustryInvoice>"
    )

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.attachments["factur-x.xml"] = pikepdf.AttachedFileSpec(
        pdf,
        generic_cii,
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    out = tmp_path / "generic.pdf"
    pdf.save(str(out))

    with ZUGFeRDParser() as parser, pytest.raises(ParseError) as excinfo:
        parser.parse(out, "application/pdf", produce_archive=False)
    msg = str(excinfo.value)
    assert "urn:cen.eu:en16931:2017" in msg

# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the cheap, content-based detection callbacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_erechnung.detection import is_xrechnung_xml
from paperless_erechnung.detection import is_zugferd_pdf


# --------------------------------------------------------------------------- #
# is_xrechnung_xml
# --------------------------------------------------------------------------- #


def test_detects_ubl_invoice(ubl_invoice_path: Path) -> None:
    assert is_xrechnung_xml(ubl_invoice_path)


def test_detects_ubl_creditnote(ubl_creditnote_path: Path) -> None:
    assert is_xrechnung_xml(ubl_creditnote_path)


def test_detects_cii_invoice(cii_invoice_path: Path) -> None:
    assert is_xrechnung_xml(cii_invoice_path)


def test_rejects_generic_ubl_invoice_without_xrechnung_profile(tmp_path: Path) -> None:
    f = tmp_path / "generic-ubl.xml"
    f.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>
  <cbc:ID>INV-1</cbc:ID>
</Invoice>
""",
    )
    assert not is_xrechnung_xml(f)


def test_rejects_cii_invoice_without_german_profile(tmp_path: Path) -> None:
    f = tmp_path / "generic-cii.xml"
    f.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
  xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:cen.eu:en16931:2017</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
</rsm:CrossIndustryInvoice>
""",
    )
    assert not is_xrechnung_xml(f)


def test_rejects_non_invoice_xml(tmp_path: Path) -> None:
    f = tmp_path / "other.xml"
    f.write_text("<root><child/></root>")
    assert not is_xrechnung_xml(f)


def test_rejects_invalid_xml(tmp_path: Path) -> None:
    f = tmp_path / "broken.xml"
    f.write_text("not xml at all <<>>")
    assert not is_xrechnung_xml(f)


def test_rejects_nonexistent_file(tmp_path: Path) -> None:
    assert not is_xrechnung_xml(tmp_path / "missing.xml")


# --------------------------------------------------------------------------- #
# is_zugferd_pdf
# --------------------------------------------------------------------------- #
#
# A real ZUGFeRD sample is heavy to vendor; we synthesize one at runtime by
# embedding an XRechnung XML inside a freshly built PDF/A-3 via pikepdf.


@pytest.fixture
def zugferd_pdf_path(tmp_path: Path, cii_invoice_bytes: bytes) -> Path:
    """Build a minimal hybrid PDF with a factur-x.xml attachment."""
    pikepdf = pytest.importorskip("pikepdf")
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


def test_detects_zugferd_pdf(zugferd_pdf_path: Path) -> None:
    assert is_zugferd_pdf(zugferd_pdf_path)


def test_rejects_plain_pdf(tmp_path: Path) -> None:
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    out = tmp_path / "plain.pdf"
    pdf.save(str(out))
    assert not is_zugferd_pdf(out)


def test_rejects_non_pdf(tmp_path: Path) -> None:
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"this is not a PDF")
    assert not is_zugferd_pdf(f)


def test_rejects_zugferd_with_invalid_xml_attachment(tmp_path: Path) -> None:
    """A correctly-named attachment with non-invoice XML must not match.

    Without validation, ``factur-x.xml`` containing ``<root/>`` outranks
    the built-in PDF parser and skips OCR — see detection.py docstring.
    """
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    attachment = pikepdf.AttachedFileSpec(
        pdf,
        b"<root/>",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments["factur-x.xml"] = attachment

    out = tmp_path / "fake-zugferd.pdf"
    pdf.save(str(out))
    assert not is_zugferd_pdf(out)


def test_rejects_zugferd_with_generic_cii_attachment(tmp_path: Path) -> None:
    """CII syntax with a generic EN16931 identifier is not enough to win."""
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    attachment = pikepdf.AttachedFileSpec(
        pdf,
        b"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
  xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:cen.eu:en16931:2017</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
</rsm:CrossIndustryInvoice>
""",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments["factur-x.xml"] = attachment

    out = tmp_path / "generic-cii.pdf"
    pdf.save(str(out))
    assert not is_zugferd_pdf(out)


def test_rejects_zugferd_with_non_xml_attachment(tmp_path: Path) -> None:
    """A spec-named attachment whose payload isn't XML must not match."""
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    attachment = pikepdf.AttachedFileSpec(
        pdf,
        b"not xml at all <<>>",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments["factur-x.xml"] = attachment

    out = tmp_path / "broken-zugferd.pdf"
    pdf.save(str(out))
    assert not is_zugferd_pdf(out)


def test_detects_zugferd_case_insensitive(
    tmp_path: Path,
    cii_invoice_bytes: bytes,
) -> None:
    """Embedded filename casing must not matter (some producers use ZUGFeRD-invoice.xml)."""
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    attachment = pikepdf.AttachedFileSpec(
        pdf,
        cii_invoice_bytes,
        filename="ZUGFeRD-invoice.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Source"),
    )
    pdf.attachments["ZUGFeRD-invoice.xml"] = attachment

    out = tmp_path / "zugferd-old.pdf"
    pdf.save(str(out))
    assert is_zugferd_pdf(out)

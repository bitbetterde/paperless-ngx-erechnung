# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the cheap, content-based detection callbacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_ngx_erechnung.detection import ErechnungValidationError
from paperless_ngx_erechnung.detection import is_xrechnung_xml
from paperless_ngx_erechnung.detection import is_zugferd_pdf
from paperless_ngx_erechnung.detection import looks_like_erechnung_xml
from paperless_ngx_erechnung.detection import pdf_has_zugferd_attachment_name
from paperless_ngx_erechnung.detection import validate_german_erechnung_xml
from paperless_ngx_erechnung.detection import validate_zugferd_pdf


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


def test_accepts_cii_invoice_with_comfort_profile(tmp_path: Path) -> None:
    """The bare EN16931 URN is the CII COMFORT profile — a valid E-Rechnung."""
    f = tmp_path / "comfort-cii.xml"
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
    assert is_xrechnung_xml(f)


def test_rejects_cii_invoice_with_sub_en16931_profile(tmp_path: Path) -> None:
    """A sub-EN16931 profile (here MINIMUM) is not a valid German E-Rechnung."""
    f = tmp_path / "minimum-cii.xml"
    f.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
  xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:factur-x.eu:1p0:minimum</ram:ID>
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


def test_accepts_zugferd_with_comfort_cii_attachment(tmp_path: Path) -> None:
    """A Factur-X PDF carrying the EN16931 (COMFORT) profile is a valid match."""
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

    out = tmp_path / "comfort-cii.pdf"
    pdf.save(str(out))
    assert is_zugferd_pdf(out)


def test_rejects_zugferd_with_sub_en16931_cii_attachment(tmp_path: Path) -> None:
    """A sub-EN16931 profile (MINIMUM) must not win — it's only a booking aid."""
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
      <ram:ID>urn:factur-x.eu:1p0:minimum</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
</rsm:CrossIndustryInvoice>
""",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments["factur-x.xml"] = attachment

    out = tmp_path / "minimum-cii.pdf"
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


# --------------------------------------------------------------------------- #
# Sniff helpers — score() uses these to claim files for detailed validation.
# --------------------------------------------------------------------------- #


def test_sniff_matches_ubl_namespace() -> None:
    assert looks_like_erechnung_xml(
        b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>',
    )


def test_sniff_matches_cii_namespace() -> None:
    assert looks_like_erechnung_xml(
        b"<rsm:CrossIndustryInvoice "
        b'xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"/>',
    )


def test_sniff_misses_unrelated_xml() -> None:
    assert not looks_like_erechnung_xml(b"<config><logger level='INFO'/></config>")


def test_sniff_misses_xml_with_namespace_past_4kb() -> None:
    # Marker beyond the sniff window — we intentionally don't read further.
    payload = (
        b"<!--"
        + b" " * 5000
        + b"-->"
        + b"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
    )
    assert not looks_like_erechnung_xml(payload)


def test_pdf_attachment_name_sniff_finds_factur_x(
    tmp_path: Path,
    cii_invoice_bytes: bytes,
) -> None:
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
    out = tmp_path / "z.pdf"
    pdf.save(str(out))
    assert pdf_has_zugferd_attachment_name(out)


def test_pdf_attachment_name_sniff_ignores_plain_pdf(tmp_path: Path) -> None:
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    out = tmp_path / "plain.pdf"
    pdf.save(str(out))
    assert not pdf_has_zugferd_attachment_name(out)


def test_pdf_attachment_name_sniff_accepts_broken_attachment_content(
    tmp_path: Path,
) -> None:
    """Sniff must match on filename alone — broken content is parse()'s problem."""
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
    out = tmp_path / "stub.pdf"
    pdf.save(str(out))
    assert pdf_has_zugferd_attachment_name(out)


# --------------------------------------------------------------------------- #
# validate_german_erechnung_xml — message contract.
# --------------------------------------------------------------------------- #


def test_validate_xml_parse_error_carries_line_and_column() -> None:
    # The real-world bug: hard-wrapped URI inside xmlns attribute value.
    bad = (
        b'<?xml version="1.0"?>\n'
        b'<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:Cross\n'
        b'IndustryInvoice:100"/>'
    )
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_german_erechnung_xml(bad)
    msg = str(excinfo.value)
    assert "XML parse error" in msg
    assert "line " in msg
    assert "column " in msg


def test_validate_xml_unexpected_root_carries_local_name() -> None:
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_german_erechnung_xml(b"<config><logger/></config>")
    assert "Unexpected XML root" in str(excinfo.value)
    assert "<config>" in str(excinfo.value)


def test_validate_ubl_without_customizationid_explains() -> None:
    bad = (
        b'<?xml version="1.0"?>\n'
        b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
    )
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_german_erechnung_xml(bad)
    assert "CustomizationID" in str(excinfo.value)


def test_validate_ubl_generic_en16931_explains() -> None:
    bad = (
        b'<?xml version="1.0"?>\n'
        b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"\n'
        b'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">\n'
        b"<cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID></Invoice>"
    )
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_german_erechnung_xml(bad)
    msg = str(excinfo.value)
    assert "urn:cen.eu:en16931:2017" in msg
    assert "xrechnung" in msg.lower()


def test_validate_cii_with_basic_profile_explains() -> None:
    bad = (
        b'<?xml version="1.0"?>\n'
        b"<rsm:CrossIndustryInvoice\n"
        b'xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"\n'
        b'xmlns:ram="urn:un:unece:uncefact:data:standard:'
        b'ReusableAggregateBusinessInformationEntity:100">'
        b"<rsm:ExchangedDocumentContext>"
        b"<ram:GuidelineSpecifiedDocumentContextParameter>"
        b"<ram:ID>urn:factur-x.eu:1p0:basic</ram:ID>"
        b"</ram:GuidelineSpecifiedDocumentContextParameter>"
        b"</rsm:ExchangedDocumentContext>"
        b"</rsm:CrossIndustryInvoice>"
    )
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_german_erechnung_xml(bad)
    msg = str(excinfo.value)
    assert "urn:factur-x.eu:1p0:basic" in msg
    assert "EN16931" in msg or "MINIMUM" in msg or "BASIC" in msg


@pytest.mark.parametrize(
    "profile_id",
    [
        # EN16931 (COMFORT) — the bare CEN URN with no vendor suffix.
        "urn:cen.eu:en16931:2017",
        # Real-world composite URNs that wrap the Factur-X/ZUGFeRD spec URN.
        "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended",
        "urn:cen.eu:en16931:2017#conformant#urn:zugferd.de:2p0:extended",
    ],
)
def test_validate_cii_en16931_and_extended_profiles_accepted(profile_id: str) -> None:
    """Factur-X/ZUGFeRD COMFORT and EXTENDED are valid German E-Rechnungen.

    EXTENDED uses a composite profile URN — see issue #2.
    """
    xml = (
        b'<?xml version="1.0"?>\n'
        b"<rsm:CrossIndustryInvoice\n"
        b'xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"\n'
        b'xmlns:ram="urn:un:unece:uncefact:data:standard:'
        b'ReusableAggregateBusinessInformationEntity:100">'
        b"<rsm:ExchangedDocumentContext>"
        b"<ram:GuidelineSpecifiedDocumentContextParameter>"
        b"<ram:ID>" + profile_id.encode() + b"</ram:ID>"
        b"</ram:GuidelineSpecifiedDocumentContextParameter>"
        b"</rsm:ExchangedDocumentContext>"
        b"</rsm:CrossIndustryInvoice>"
    )
    validate_german_erechnung_xml(xml)  # no exception


def test_validate_valid_invoice_does_not_raise(cii_invoice_bytes: bytes) -> None:
    validate_german_erechnung_xml(cii_invoice_bytes)  # no exception


# --------------------------------------------------------------------------- #
# validate_zugferd_pdf — message contract.
# --------------------------------------------------------------------------- #


def test_validate_pdf_no_attachment(tmp_path: Path) -> None:
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    out = tmp_path / "plain.pdf"
    pdf.save(str(out))
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_zugferd_pdf(out)
    msg = str(excinfo.value)
    assert (
        "no Factur-X / ZUGFeRD attachment" in msg.lower()
        or "no factur-x" in msg.lower()
    )


def test_validate_pdf_stub_attachment_explains(tmp_path: Path) -> None:
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    spec = pikepdf.AttachedFileSpec(
        pdf,
        b"<root/>",
        filename="factur-x.xml",
        mime_type="text/xml",
        relationship=pikepdf.Name("/Alternative"),
    )
    pdf.attachments["factur-x.xml"] = spec
    out = tmp_path / "stub.pdf"
    pdf.save(str(out))
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_zugferd_pdf(out)
    msg = str(excinfo.value)
    assert "factur-x.xml" in msg
    assert "Unexpected XML root" in msg


def test_validate_pdf_malformed_pdf_bytes(tmp_path: Path) -> None:
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"this is not a PDF at all")
    with pytest.raises(ErechnungValidationError) as excinfo:
        validate_zugferd_pdf(f)
    assert "PDF could not be" in str(excinfo.value)

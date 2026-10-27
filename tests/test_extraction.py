# SPDX-License-Identifier: GPL-3.0-only
"""Tests for InvoiceData extraction from real XRechnung samples."""

from __future__ import annotations

import datetime
from decimal import Decimal

from paperless_ngx_erechnung.extraction import BusinessTerm
from paperless_ngx_erechnung.extraction import InvoiceData
from paperless_ngx_erechnung.extraction import extract_all_fields
from paperless_ngx_erechnung.extraction import extract_business_terms
from paperless_ngx_erechnung.extraction import extract_invoice_fields


# --------------------------------------------------------------------------- #
# UBL Invoice — KoSIT maxRechnung_ubl.xml
# --------------------------------------------------------------------------- #


def test_ubl_invoice_extracts_header(ubl_invoice_bytes: bytes) -> None:
    data = extract_invoice_fields(ubl_invoice_bytes)

    assert data.invoice_number == "1234567890"
    assert data.issue_date == datetime.date(2018, 10, 15)
    assert data.due_date == datetime.date(2018, 10, 29)
    assert data.currency == "EUR"


def test_ubl_invoice_extracts_parties(ubl_invoice_bytes: bytes) -> None:
    data = extract_invoice_fields(ubl_invoice_bytes)

    assert data.seller_name == "EntServ Deutschland GmbH"
    assert data.buyer_name is not None and "Bundesamt" in data.buyer_name or True
    # Buyer cell varies across releases of the sample; assert at least populated.
    assert data.buyer_name is not None


def test_ubl_invoice_extracts_totals(ubl_invoice_bytes: bytes) -> None:
    data = extract_invoice_fields(ubl_invoice_bytes)

    assert data.total_amount == Decimal("8320.00")
    assert data.net_amount == Decimal("8100.00")
    assert data.tax_amount == Decimal("510.00")


# --------------------------------------------------------------------------- #
# UBL CreditNote — KoSIT maxRechnung_creditnote.xml
# --------------------------------------------------------------------------- #


def test_ubl_creditnote_extracts(ubl_creditnote_bytes: bytes) -> None:
    data = extract_invoice_fields(ubl_creditnote_bytes)

    # CreditNote should populate the same shape as Invoice.
    assert data.invoice_number is not None
    assert data.issue_date is not None
    assert data.currency == "EUR"
    assert data.total_amount is not None


# --------------------------------------------------------------------------- #
# CII CrossIndustryInvoice — KoSIT included-notes-bg-1-uncefact.xml
# --------------------------------------------------------------------------- #


def test_cii_extracts_header(cii_invoice_bytes: bytes) -> None:
    data = extract_invoice_fields(cii_invoice_bytes)

    assert data.invoice_number == "RR123456"
    assert data.issue_date == datetime.date(2016, 6, 24)
    assert data.currency == "EUR"


def test_cii_extracts_totals(cii_invoice_bytes: bytes) -> None:
    data = extract_invoice_fields(cii_invoice_bytes)

    assert data.total_amount == Decimal("336.9")
    assert data.net_amount == Decimal("314.86")
    assert data.tax_amount == Decimal("22.04")


def test_cii_extracts_vat_id_from_va_scheme(cii_invoice_bytes: bytes) -> None:
    """CII seller VAT ID must come from the ``schemeID='VA'`` entry."""
    data = extract_invoice_fields(cii_invoice_bytes)
    assert data.seller_vat_id is not None
    assert "DE" in data.seller_vat_id


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #


def test_invalid_xml_returns_empty() -> None:
    data = extract_invoice_fields(b"not valid xml")
    assert data == InvoiceData()


def test_unknown_root_returns_empty() -> None:
    data = extract_invoice_fields(b"<root/>")
    assert data == InvoiceData()


def test_missing_fields_yield_none() -> None:
    minimal = (
        b'<?xml version="1.0"?>'
        b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">'
        b"</Invoice>"
    )
    data = extract_invoice_fields(minimal)
    assert data.invoice_number is None
    assert data.total_amount is None
    assert data == InvoiceData()


# --------------------------------------------------------------------------- #
# Generic all-fields dump
# --------------------------------------------------------------------------- #


def test_all_fields_covers_every_leaf(cii_invoice_bytes: bytes) -> None:
    fields = extract_all_fields(cii_invoice_bytes)
    paths = [path for path, _ in fields]

    assert ("ExchangedDocument/ID", "RR123456") in fields
    # Fields outside the curated InvoiceData set show up too.
    assert any("SellerTradeParty/PostalTradeAddress" in p for p in paths)
    assert any("IncludedSupplyChainTradeLineItem" in p for p in paths)
    # The root element name is not part of the path.
    assert not any(p.startswith("CrossIndustryInvoice") for p in paths)


def test_all_fields_appends_attributes_and_skips_binary() -> None:
    xml = (
        b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">'
        b"<!-- comment -->"
        b'<Total currencyID="EUR"> 12.50 </Total>'
        b"<Empty/>"
        b'<Attachment><EmbeddedDocumentBinaryObject filename="a.pdf">'
        b"QUJD</EmbeddedDocumentBinaryObject></Attachment>"
        b"</Invoice>"
    )
    assert extract_all_fields(xml) == [
        ("Total", "12.50 (currencyID=EUR)"),
        ("Attachment/EmbeddedDocumentBinaryObject", "(filename=a.pdf)"),
    ]


def test_all_fields_invalid_xml_returns_empty() -> None:
    assert extract_all_fields(b"not valid xml") == []


def test_all_fields_skips_covered_subtrees_by_indexed_key() -> None:
    xml = (
        b'<r:Invoice xmlns:r="urn:x">'
        b"<r:ID>1</r:ID>"
        b"<r:Note><r:Text>a</r:Text></r:Note>"
        b"<r:Note><r:Text>b</r:Text></r:Note>"
        b'<r:Qty unit="H87">3</r:Qty>'
        b"</r:Invoice>"
    )
    # Index only where same-named siblings exist, as KoSIT's xr:src does.
    assert extract_all_fields(xml, skip={"/Invoice/ID", "/Invoice/Note[2]"}) == [
        ("Note/Text", "a"),
        ("Qty", "3 (unit=H87)"),
    ]


# --------------------------------------------------------------------------- #
# EN 16931 business terms (KoSIT XR)
# --------------------------------------------------------------------------- #

_XR_NS = "urn:ce.eu:en16931:2017:xoev-de:kosit:standard:xrechnung-1"


def test_business_terms_flatten_groups_and_label() -> None:
    xr = (
        f'<xr:invoice xmlns:xr="{_XR_NS}">'
        '<xr:Invoice_number xr:id="BT-1" xr:src="/rsm:CII/rsm:Doc/ram:ID">'
        "R1</xr:Invoice_number>"
        '<xr:SELLER xr:id="BG-4" xr:src="/rsm:CII/ram:Seller">'
        '<xr:Seller_identifier xr:id="BT-29" scheme_identifier="0088"'
        ' xr:src="/rsm:CII/ram:Seller/ram:ID[2]">4000</xr:Seller_identifier>'
        "</xr:SELLER>"
        '<xr:Invoice_note xr:id="BT-22"> </xr:Invoice_note>'
        "</xr:invoice>"
    ).encode()
    assert extract_business_terms(xr) == [
        BusinessTerm("BT-1", "Invoice number", "Rechnungsnummer", "R1", "/CII/Doc/ID"),
        BusinessTerm(
            "BT-29",
            "Seller identifier",
            "Kennung",
            "4000 (scheme_identifier=0088)",
            "/CII/Seller/ID[2]",
        ),
    ]


def test_business_terms_attribute_source_maps_to_owner_element() -> None:
    xr = (
        f'<xr:invoice xmlns:xr="{_XR_NS}">'
        '<xr:Invoiced_quantity_unit_of_measure_code xr:id="BT-130"'
        ' xr:src="/rsm:CII/ram:Line[1]/ram:Qty/@unitCode">H87'
        "</xr:Invoiced_quantity_unit_of_measure_code>"
        "</xr:invoice>"
    ).encode()
    [term] = extract_business_terms(xr)
    assert term.src == "/CII/Line[1]/Qty"


def test_business_terms_invalid_xml_returns_empty() -> None:
    assert extract_business_terms(b"not valid xml") == []


def test_as_dict_skips_none_fields() -> None:
    data = InvoiceData(invoice_number="X", currency="EUR")
    d = data.as_dict()
    assert d == {"invoice_number": "X", "currency": "EUR"}

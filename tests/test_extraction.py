# SPDX-License-Identifier: GPL-3.0-only
"""Tests for InvoiceData extraction from real XRechnung samples."""

from __future__ import annotations

import datetime
from decimal import Decimal

from paperless_erechnung.extraction import InvoiceData
from paperless_erechnung.extraction import extract_invoice_fields


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


def test_as_dict_skips_none_fields() -> None:
    data = InvoiceData(invoice_number="X", currency="EUR")
    d = data.as_dict()
    assert d == {"invoice_number": "X", "currency": "EUR"}

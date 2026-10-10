# SPDX-License-Identifier: GPL-3.0-only
"""Extract EN 16931 invoice fields from XRechnung / Factur-X XML.

Supports two source syntaxes:

- **UBL** — both ``Invoice`` and ``CreditNote`` roots, namespace
  ``urn:oasis:names:specification:ubl:schema:xsd:*``.
- **UN/CEFACT CII** — ``CrossIndustryInvoice`` root, namespace
  ``urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100``.

Both are normalized to a common :class:`InvoiceData` dataclass. Missing
fields are tolerated and yield ``None`` rather than raising — the MINIMUM
and BASIC WL Factur-X profiles legitimately omit several fields.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation

from lxml import etree

logger = logging.getLogger("paperless_ngx_erechnung.extraction")


# --------------------------------------------------------------------------- #
# Namespace maps
# --------------------------------------------------------------------------- #

_NS_UBL_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
_NS_UBL_CREDITNOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
_NS_CII = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"

# Common UBL building blocks (cac / cbc).
_NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
_NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

# CII building blocks (ram / udt).
_NS_RAM = (
    "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
)
_NS_UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"

_UBL_NSMAP = {"cac": _NS_CAC, "cbc": _NS_CBC}
_CII_NSMAP = {"rsm": _NS_CII, "ram": _NS_RAM, "udt": _NS_UDT}


# --------------------------------------------------------------------------- #
# Public data type
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class InvoiceData:
    """Normalized EN 16931 invoice fields. All optional."""

    invoice_number: str | None = None
    issue_date: datetime.date | None = None
    due_date: datetime.date | None = None
    seller_name: str | None = None
    seller_vat_id: str | None = None
    buyer_name: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None

    def as_dict(self) -> dict[str, str]:
        """Return all populated fields as ``{key: str(value)}`` pairs.

        Used by the parser to build the searchable text block and the
        metadata sidebar entries.
        """
        out: dict[str, str] = {}
        for key, value in asdict(self).items():
            if value is None:
                continue
            out[key] = str(value)
        return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def extract_invoice_fields(xml_bytes: bytes) -> InvoiceData:
    """Parse *xml_bytes* and return a populated :class:`InvoiceData`.

    Dispatches to a UBL- or CII-specific extractor based on the document's
    root element namespace. Returns an empty :class:`InvoiceData` (all
    ``None``) when the document is not a recognised invoice XML — the
    caller decides whether that's an error.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        logger.warning("Could not parse invoice XML: %s", exc)
        return InvoiceData()

    tag = root.tag
    if tag == f"{{{_NS_UBL_INVOICE}}}Invoice":
        return _extract_ubl(root, is_credit_note=False)
    if tag == f"{{{_NS_UBL_CREDITNOTE}}}CreditNote":
        return _extract_ubl(root, is_credit_note=True)
    if tag == f"{{{_NS_CII}}}CrossIndustryInvoice":
        return _extract_cii(root)

    logger.info("Unrecognised invoice root element %r — returning empty data.", tag)
    return InvoiceData()


def extract_all_fields(xml_bytes: bytes) -> list[tuple[str, str]]:
    """Return every populated leaf element of *xml_bytes* as ``(path, value)``.

    Unlike :func:`extract_invoice_fields` this is syntax-agnostic: it walks
    the whole tree in document order and yields one entry per element that
    carries text or attributes, keyed by its slash-joined local-name path
    below the root (e.g. ``ExchangedDocument/ID``). Attributes such as
    ``currencyID`` / ``unitCode`` are appended to the value in parentheses.
    Repeated elements (invoice lines, tax breakdowns) yield repeated keys.

    Returns an empty list when the XML cannot be parsed.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        logger.warning("Could not parse invoice XML: %s", exc)
        return []

    fields: list[tuple[str, str]] = []
    _collect_leaves(root, "", fields)
    return fields


def _collect_leaves(
    element: etree._Element,
    path: str,
    out: list[tuple[str, str]],
) -> None:
    # Comments and processing instructions have a non-string tag.
    children = [c for c in element if isinstance(c.tag, str)]
    if children:
        for child in children:
            name = etree.QName(child).localname
            _collect_leaves(child, f"{path}/{name}" if path else name, out)
        return
    if not path:
        return

    # Embedded attachments are base64 blobs — keep their attributes
    # (filename, mimeCode) but never dump the payload into the text index.
    is_binary = path.endswith("BinaryObject")
    text = "" if is_binary else " ".join((element.text or "").split())
    attrs = ", ".join(
        f"{etree.QName(key).localname}={value}"
        for key, value in element.attrib.items()
    )
    if attrs:
        text = f"{text} ({attrs})" if text else f"({attrs})"
    if text:
        out.append((path, text))


# --------------------------------------------------------------------------- #
# UBL extractor (Invoice + CreditNote)
# --------------------------------------------------------------------------- #


def _extract_ubl(root: etree._Element, *, is_credit_note: bool) -> InvoiceData:
    get = lambda xpath: _first_text(root, xpath, _UBL_NSMAP)  # noqa: E731

    # CreditNotes label the issue date the same way as Invoices, but
    # totals live under cac:LegalMonetaryTotal for both.
    return InvoiceData(
        invoice_number=get("cbc:ID"),
        issue_date=_parse_date(get("cbc:IssueDate")),
        due_date=_parse_date(get("cbc:DueDate")),
        seller_name=get(
            "cac:AccountingSupplierParty/cac:Party"
            "/cac:PartyLegalEntity/cbc:RegistrationName",
        )
        or get(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyName/cbc:Name",
        ),
        seller_vat_id=get(
            "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
        ),
        buyer_name=get(
            "cac:AccountingCustomerParty/cac:Party"
            "/cac:PartyLegalEntity/cbc:RegistrationName",
        )
        or get(
            "cac:AccountingCustomerParty/cac:Party/cac:PartyName/cbc:Name",
        ),
        currency=get("cbc:DocumentCurrencyCode"),
        total_amount=_parse_decimal(
            get("cac:LegalMonetaryTotal/cbc:PayableAmount"),
        ),
        net_amount=_parse_decimal(
            get("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"),
        ),
        tax_amount=_parse_decimal(get("cac:TaxTotal/cbc:TaxAmount")),
    )


# --------------------------------------------------------------------------- #
# CII extractor
# --------------------------------------------------------------------------- #


def _extract_cii(root: etree._Element) -> InvoiceData:
    get = lambda xpath: _first_text(root, xpath, _CII_NSMAP)  # noqa: E731

    header = "rsm:ExchangedDocument/"
    agreement = "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeAgreement/"
    settlement = "rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeSettlement/"
    monetary = f"{settlement}ram:SpecifiedTradeSettlementHeaderMonetarySummation/"

    issue = get(
        f"{header}ram:IssueDateTime/udt:DateTimeString",
    )

    return InvoiceData(
        invoice_number=get(f"{header}ram:ID"),
        issue_date=_parse_cii_date(issue),
        due_date=_parse_cii_date(
            get(
                f"{settlement}ram:SpecifiedTradePaymentTerms/"
                "ram:DueDateDateTime/udt:DateTimeString",
            ),
        ),
        seller_name=get(f"{agreement}ram:SellerTradeParty/ram:Name"),
        seller_vat_id=get(
            f"{agreement}ram:SellerTradeParty/"
            "ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']",
        )
        or get(
            f"{agreement}ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID",
        ),
        buyer_name=get(f"{agreement}ram:BuyerTradeParty/ram:Name"),
        currency=get(f"{settlement}ram:InvoiceCurrencyCode"),
        total_amount=_parse_decimal(get(f"{monetary}ram:GrandTotalAmount")),
        net_amount=_parse_decimal(get(f"{monetary}ram:TaxBasisTotalAmount")),
        tax_amount=_parse_decimal(get(f"{monetary}ram:TaxTotalAmount")),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _first_text(
    root: etree._Element,
    xpath: str,
    nsmap: dict[str, str],
) -> str | None:
    """Return the first matching element's text content, stripped, or None."""
    nodes = root.xpath(xpath, namespaces=nsmap)
    if not nodes:
        return None
    node = nodes[0]
    if isinstance(node, str):
        return node.strip() or None
    text = node.text
    return text.strip() if text and text.strip() else None


def _parse_date(value: str | None) -> datetime.date | None:
    """Parse ISO-8601 date (UBL form: ``YYYY-MM-DD``). Returns None on failure."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_cii_date(value: str | None) -> datetime.date | None:
    """Parse CII ``DateTimeString`` with format=102 (``YYYYMMDD``).

    CII dates are typically eight-digit strings in ``udt:DateTimeString``.
    Fall back to ISO-8601 if the input looks dashed.
    """
    if not value:
        return None
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        try:
            return datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        except ValueError:
            return None
    return _parse_date(value)


def _parse_decimal(value: str | None) -> Decimal | None:
    """Parse a monetary amount, tolerant of whitespace; None on failure."""
    if value is None:
        return None
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None

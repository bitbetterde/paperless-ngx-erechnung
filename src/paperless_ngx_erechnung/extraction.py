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
import functools
import logging
from collections.abc import Collection
from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

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

# KoSIT's intermediate "XR" format (output of the stage-1 normalizer XSLT).
_NS_XR = "urn:ce.eu:en16931:2017:xoev-de:kosit:standard:xrechnung-1"
_XR_ID = f"{{{_NS_XR}}}id"
_XR_SRC = f"{{{_NS_XR}}}src"

# German field labels shipped with the KoSIT visualization.
_L10N_DE = Path(__file__).parent / "xslt" / "l10n" / "de.xml"


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


@dataclass(frozen=True, slots=True)
class BusinessTerm:
    """One EN 16931 business term (BT) value from a KoSIT XR document."""

    bt_id: str  # "BT-22"
    name: str  # "Invoice note"
    label_de: str | None  # "Bemerkung"
    value: str
    # Source node this value came from, as a path of local names with
    # positional indices; see _source_key().
    src: str | None


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


def extract_all_fields(
    xml_bytes: bytes,
    skip: Collection[str] = (),
) -> list[tuple[str, str]]:
    """Return every populated leaf element of *xml_bytes* as ``(path, value)``.

    Unlike :func:`extract_invoice_fields` this is syntax-agnostic: it walks
    the whole tree in document order and yields one entry per element that
    carries text or attributes, keyed by its slash-joined local-name path
    below the root (e.g. ``ExchangedDocument/ID``). Attributes such as
    ``currencyID`` / ``unitCode`` are appended to the value in parentheses.
    Repeated elements (invoice lines, tax breakdowns) yield repeated keys.

    Elements whose source key (see :attr:`BusinessTerm.src`) is in *skip*
    are left out together with their descendants — used to drop fields
    already listed as business terms.

    Returns an empty list when the XML cannot be parsed.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        logger.warning("Could not parse invoice XML: %s", exc)
        return []

    fields: list[tuple[str, str]] = []
    root_key = "/" + etree.QName(root).localname
    _collect_leaves(root, "", root_key, fields, frozenset(skip))
    return fields


def extract_business_terms(xr_bytes: bytes) -> list[BusinessTerm]:
    """Return every populated business term of a KoSIT XR document.

    *xr_bytes* is the output of the KoSIT normalizer XSLT
    (:func:`paperless_ngx_erechnung.rendering.normalize_to_xr`), in which
    every value is tagged with its EN 16931 ID (``BT-…``). Group elements
    (``BG-…``) are flattened away. Returns an empty list when the XML
    cannot be parsed.
    """
    try:
        root = etree.fromstring(xr_bytes)
    except etree.XMLSyntaxError as exc:
        logger.warning("Could not parse KoSIT XR XML: %s", exc)
        return []

    labels = _german_labels()
    terms: list[BusinessTerm] = []
    for element in root.iter(f"{{{_NS_XR}}}*"):
        bt_id = element.get(_XR_ID)
        if not bt_id or any(isinstance(c.tag, str) for c in element):
            continue
        value = " ".join((element.text or "").split())
        attrs = ", ".join(
            f"{etree.QName(key).localname}={val}"
            for key, val in element.attrib.items()
            if not key.startswith(f"{{{_NS_XR}}}")
        )
        if attrs:
            value = f"{value} ({attrs})" if value else f"({attrs})"
        if not value:
            continue
        local = etree.QName(element).localname
        src = element.get(_XR_SRC)
        terms.append(
            BusinessTerm(
                bt_id=bt_id,
                name=local.replace("_", " "),
                label_de=labels.get(local),
                value=value,
                src=_source_key(src) if src else None,
            ),
        )
    return terms


@functools.cache
def _german_labels() -> dict[str, str]:
    """Map XR element local names to KoSIT's German labels (``l10n/de.xml``)."""
    try:
        root = etree.parse(str(_L10N_DE)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return {}
    labels: dict[str, str] = {}
    for entry in root.iter("entry"):
        key = entry.get("key") or ""
        text = " ".join((entry.text or "").split())
        if key.startswith("xr:") and "/" not in key and text:
            labels[key[3:]] = text
    return labels


def _source_key(src: str) -> str:
    """Normalize a KoSIT ``xr:src`` XPath to the key used by _collect_leaves.

    KoSIT writes ``/rsm:A/ram:B[2]/ram:C`` (prefixes as in the source
    document, an index only where same-named siblings exist). We drop the
    prefixes, and map an attribute (``…/@unitCode``) to its owner element.
    """
    steps = [s for s in src.split("/") if s]
    if steps and steps[-1].startswith("@"):
        steps.pop()
    return "/" + "/".join(s.split(":", 1)[-1] for s in steps)


def _collect_leaves(
    element: etree._Element,
    path: str,
    src_key: str,
    out: list[tuple[str, str]],
    skip: frozenset[str],
) -> None:
    if src_key in skip:
        return
    # Comments and processing instructions have a non-string tag.
    children = [c for c in element if isinstance(c.tag, str)]
    if children:
        counts: dict[str, int] = {}
        for child in children:
            counts[child.tag] = counts.get(child.tag, 0) + 1
        seen: dict[str, int] = {}
        for child in children:
            name = etree.QName(child).localname
            # Same indexing rule as KoSIT's xr:src-path: only number
            # elements that have same-named siblings.
            step = name
            if counts[child.tag] > 1:
                seen[child.tag] = seen.get(child.tag, 0) + 1
                step = f"{name}[{seen[child.tag]}]"
            _collect_leaves(
                child,
                f"{path}/{name}" if path else name,
                f"{src_key}/{step}",
                out,
                skip,
            )
        return
    if not path:
        return

    # Embedded attachments are base64 blobs — keep their attributes
    # (filename, mimeCode) but never dump the payload into the text index.
    is_binary = path.endswith("BinaryObject")
    text = "" if is_binary else " ".join((element.text or "").split())
    attrs = ", ".join(
        f"{etree.QName(key).localname}={value}" for key, value in element.attrib.items()
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

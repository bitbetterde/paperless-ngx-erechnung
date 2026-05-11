# SPDX-License-Identifier: GPL-3.0-only
"""Cheap content-based detection for XRechnung XML and ZUGFeRD/Factur-X PDFs.

Both functions are called from a parser's ``score()`` classmethod, which the
Paperless-ngx registry invokes for every ingested file whose MIME type
matches the parser's declared list. They must be fast and side-effect-free.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree

if TYPE_CHECKING:
    from pikepdf import Pdf

logger = logging.getLogger("paperless_erechnung.detection")


# Namespaces that mark an XML file as EN 16931 invoice syntax. Syntax alone is
# not enough for plugin dispatch; the profile checks below must also match a
# German E-Rechnung profile.
_XRECHNUNG_ROOT_TAGS: frozenset[str] = frozenset(
    {
        "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice",
        "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote",
        "{urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100}CrossIndustryInvoice",
    },
)

_NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
_NS_CII = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
_NS_RAM = (
    "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
)

_XML_PARSER = etree.XMLParser(
    huge_tree=False,
    no_network=True,
    recover=False,
    resolve_entities=False,
)

_UBL_NSMAP = {"cbc": _NS_CBC}
_CII_NSMAP = {"rsm": _NS_CII, "ram": _NS_RAM}

# XRechnung is the German CIUS. Factur-X/ZUGFeRD profiles below EN16931
# (MINIMUM, BASIC WL, BASIC) are intentionally not accepted here.
_XRECHNUNG_PROFILE_MARKER = "xrechnung"
_FACTUR_X_EN16931_PROFILE_MARKERS = frozenset(
    {
        "urn:factur-x.eu:1p0:en16931",
        "urn:factur-x.eu:1p0:extended",
        "urn:zugferd.de:2p0:en16931",
        "urn:zugferd.de:2p0:extended",
    },
)


# Embedded-file names defined by the Factur-X / ZUGFeRD specifications.
# The PDF/A-3 attachment name tree should contain one of these (case-insensitive).
_ZUGFERD_EMBED_NAMES: frozenset[str] = frozenset(
    {"factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml"},
)


def is_xrechnung_xml(path: Path) -> bool:
    """Return True if *path* is an XRechnung-compatible XML invoice.

    Any I/O or XML error returns False rather than raising, because this runs
    in the parser's ``score()`` callback and must never crash the dispatch loop.
    """
    try:
        return is_german_erechnung_xml(Path(path).read_bytes())
    except OSError:
        return False


def is_german_erechnung_xml(xml_bytes: bytes) -> bool:
    """Return True if *xml_bytes* declares a supported German E-Rechnung profile."""
    try:
        root = etree.fromstring(xml_bytes, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return False

    if root.tag not in _XRECHNUNG_ROOT_TAGS:
        return False

    if root.tag.startswith("{urn:oasis:names:specification:ubl:schema:xsd:"):
        return _has_ubl_xrechnung_profile(root)

    if root.tag == f"{{{_NS_CII}}}CrossIndustryInvoice":
        return _has_cii_german_erechnung_profile(root)

    return False


def _has_ubl_xrechnung_profile(root: etree._Element) -> bool:
    ids = root.xpath("cbc:CustomizationID/text()", namespaces=_UBL_NSMAP)
    return any(_XRECHNUNG_PROFILE_MARKER in value.lower() for value in ids)


def _has_cii_german_erechnung_profile(root: etree._Element) -> bool:
    ids = root.xpath(
        "rsm:ExchangedDocumentContext/"
        "ram:GuidelineSpecifiedDocumentContextParameter/"
        "ram:ID/text()",
        namespaces=_CII_NSMAP,
    )
    for value in ids:
        normalized = value.strip().lower()
        if _XRECHNUNG_PROFILE_MARKER in normalized:
            return True
        if normalized in _FACTUR_X_EN16931_PROFILE_MARKERS:
            return True
    return False


def is_zugferd_pdf(path: Path) -> bool:
    """Return True if *path* is a ZUGFeRD / Factur-X hybrid PDF.

    Opens the PDF with pikepdf, walks the embedded-file name tree for a
    spec-mandated attachment name (case-insensitive), and verifies the
    attachment is XML declaring a supported German E-Rechnung profile.
    Filename alone is not enough: a stub like ``<root/>`` would otherwise
    outrank the built-in PDF parser and suppress OCR. Any error returns
    False.
    """
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - pikepdf is a hard dep
        logger.warning("pikepdf is not installed; ZUGFeRD detection disabled.")
        return False

    try:
        with pikepdf.open(str(path)) as pdf:
            return _has_zugferd_attachment(pdf)
    except (pikepdf.PdfError, OSError):
        return False


def _has_zugferd_attachment(pdf: Pdf) -> bool:
    """Walk a PDF's embedded-file name tree looking for an E-Rechnung XML."""
    # pikepdf's attachments mapping mirrors the /Names/EmbeddedFiles tree.
    try:
        names = list(pdf.attachments)
    except Exception:  # pragma: no cover - defensive
        return False

    for name in names:
        if name.lower() not in _ZUGFERD_EMBED_NAMES:
            continue
        try:
            data = bytes(pdf.attachments[name].get_file().read_bytes())
        except Exception:
            continue
        if is_german_erechnung_xml(data):
            return True
    return False

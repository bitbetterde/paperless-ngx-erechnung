# SPDX-License-Identifier: GPL-3.0-only
"""Cheap content-based detection for XRechnung XML and ZUGFeRD/Factur-X PDFs.

Both functions are called from a parser's ``score()`` classmethod, which the
Paperless-ngx registry invokes for every ingested file whose MIME type
matches the parser's declared list. They must be fast and side-effect-free.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

logger = logging.getLogger("paperless_ngx_erechnung.detection")


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

# The EN 16931 (COMFORT) profile is identified by the bare CEN URN with no
# vendor suffix. In CII / Factur-X this is the canonical identifier of a
# valid German E-Rechnung, so it is accepted on the CII path. In UBL the same
# value just means "generic EN16931" rather than the German XRechnung CIUS,
# so _require_ubl_xrechnung_profile deliberately does not accept it.
_CII_EN16931_COMFORT_PROFILE = "urn:cen.eu:en16931:2017"

# EXTENDED is a superset of EN16931. Its real-world profile URN is a composite
# that wraps the Factur-X/ZUGFeRD spec URN, e.g.
# ``urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended``, so these
# markers are matched as substrings.
_FACTUR_X_EXTENDED_PROFILE_MARKERS = frozenset(
    {
        "urn:factur-x.eu:1p0:extended",
        "urn:zugferd.de:2p0:extended",
    },
)


# Embedded-file names defined by the Factur-X / ZUGFeRD specifications.
# The PDF/A-3 attachment name tree should contain one of these (case-insensitive).
_ZUGFERD_EMBED_NAMES: frozenset[str] = frozenset(
    {"factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml"},
)


# Bytes-level namespace markers used by looks_like_erechnung_xml(). Substring
# search on the first few KB of a file is enough to decide whether to claim
# it for our parser — much cheaper than parsing.
_XML_SNIFF_MARKERS: tuple[bytes, ...] = (
    b"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    b"urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
    b"urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
)
_XML_SNIFF_BYTES = 4096


class ErechnungValidationError(ValueError):
    """Validation failed for an XRechnung XML or ZUGFeRD PDF.

    Carries a human-readable, UI-safe message. The parser catches this and
    re-raises as Paperless's ParseError so the message surfaces in the UI's
    failed-document indicator.
    """


def looks_like_erechnung_xml(xml_bytes: bytes) -> bool:
    """Cheap sniff: does *xml_bytes* mention a UBL or CII invoice namespace?

    Used by ``XRechnungParser.score()`` to decide whether to *claim* a file
    so its strict validation can run in ``parse()`` and surface meaningful
    errors. False positives (non-invoice XML that happens to contain the
    string) are acceptable — they'll fail validation and produce a clear
    "wrong root" or parse-error message.
    """
    head = xml_bytes[:_XML_SNIFF_BYTES]
    return any(marker in head for marker in _XML_SNIFF_MARKERS)


def pdf_has_zugferd_attachment_name(path: Path) -> bool:
    """Cheap sniff: does *path* embed a file whose name matches the spec?

    Walks ``/Names/EmbeddedFiles`` and returns True on any case-insensitive
    name hit, regardless of whether the attachment is well-formed XML.
    ``ZUGFeRDParser.score()`` uses this to claim the file so ``parse()`` can
    raise specific errors when the attachment is broken or non-German.
    """
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - pikepdf is a hard dep
        logger.warning("pikepdf is not installed; ZUGFeRD detection disabled.")
        return False

    try:
        with pikepdf.open(str(path)) as pdf:
            try:
                names = list(pdf.attachments)
            except Exception:  # pragma: no cover - defensive
                return False
            return any(name.lower() in _ZUGFERD_EMBED_NAMES for name in names)
    except (pikepdf.PdfError, OSError):
        return False


def validate_german_erechnung_xml(xml_bytes: bytes) -> None:
    """Strict validation. Raise ErechnungValidationError with a UI-safe reason."""
    try:
        root = etree.fromstring(xml_bytes, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        # exc.lineno / exc.offset are the lxml-reported coordinates.
        line = exc.lineno or 0
        col = exc.offset or 0
        msg = f"XML parse error at line {line}, column {col}: {exc.msg}"
        raise ErechnungValidationError(msg) from exc

    if root.tag not in _XRECHNUNG_ROOT_TAGS:
        msg = (
            f"Unexpected XML root {_format_tag(root.tag)}; expected UBL "
            f"Invoice/CreditNote or CII CrossIndustryInvoice."
        )
        raise ErechnungValidationError(msg)

    if root.tag.startswith("{urn:oasis:names:specification:ubl:schema:xsd:"):
        _require_ubl_xrechnung_profile(root)
    elif root.tag == f"{{{_NS_CII}}}CrossIndustryInvoice":
        _require_cii_german_erechnung_profile(root)


def validate_zugferd_pdf(path: Path) -> bytes:
    """Open *path*, locate a spec-named attachment, and validate its XML.

    Returns the validated attachment bytes. Raises ErechnungValidationError
    with a UI-safe reason on any failure.
    """
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - pikepdf is a hard dep
        msg = "pikepdf is not installed; ZUGFeRD detection unavailable."
        raise ErechnungValidationError(msg) from None

    try:
        pdf_cm = pikepdf.open(str(path))
    except pikepdf.PdfError as exc:
        msg = f"PDF could not be opened: {exc}"
        raise ErechnungValidationError(msg) from exc
    except OSError as exc:
        msg = f"PDF could not be read: {exc}"
        raise ErechnungValidationError(msg) from exc

    with pdf_cm as pdf:
        try:
            names = list(pdf.attachments)
        except Exception as exc:  # pragma: no cover - defensive
            msg = f"PDF attachment table is unreadable: {exc}"
            raise ErechnungValidationError(msg) from exc

        matched_name: str | None = None
        for name in names:
            if name.lower() in _ZUGFERD_EMBED_NAMES:
                matched_name = name
                break

        if matched_name is None:
            expected = ", ".join(sorted(_ZUGFERD_EMBED_NAMES))
            msg = (
                f"PDF has no Factur-X / ZUGFeRD attachment "
                f"(expected one of: {expected})."
            )
            raise ErechnungValidationError(msg)

        try:
            data = bytes(pdf.attachments[matched_name].get_file().read_bytes())
        except Exception as exc:
            msg = f"Could not read embedded {matched_name!r}: {exc}"
            raise ErechnungValidationError(msg) from exc

    try:
        validate_german_erechnung_xml(data)
    except ErechnungValidationError as exc:
        msg = f"Embedded {matched_name!r}: {exc}"
        raise ErechnungValidationError(msg) from exc

    return data


# --------------------------------------------------------------------------- #
# Boolean wrappers — back-compat for callers that just want yes/no
# --------------------------------------------------------------------------- #


def is_xrechnung_xml(path: Path) -> bool:
    """Return True if *path* is a valid German E-Rechnung XML."""
    try:
        return is_german_erechnung_xml(Path(path).read_bytes())
    except OSError:
        return False


def is_german_erechnung_xml(xml_bytes: bytes) -> bool:
    """Return True if *xml_bytes* validates as a German E-Rechnung."""
    try:
        validate_german_erechnung_xml(xml_bytes)
    except ErechnungValidationError:
        return False
    return True


def is_zugferd_pdf(path: Path) -> bool:
    """Return True if *path* is a valid ZUGFeRD / Factur-X hybrid PDF."""
    try:
        validate_zugferd_pdf(path)
    except ErechnungValidationError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _format_tag(tag: str) -> str:
    """Render a Clark-notation tag as ``{namespace}localname`` for messages."""
    return f"<{tag}>" if "}" not in tag else f"<{tag.split('}', 1)[1]}>"


def _require_ubl_xrechnung_profile(root: etree._Element) -> None:
    ids = root.xpath("cbc:CustomizationID/text()", namespaces=_UBL_NSMAP)
    if not ids:
        msg = (
            "Not a German E-Rechnung: <cbc:CustomizationID> element is missing. "
            "An XRechnung invoice must declare a CIUS, e.g. "
            "urn:cen.eu:en16931:2017#compliant#urn:xoev-de:kosit:standard:xrechnung_3.0."
        )
        raise ErechnungValidationError(msg)
    if not any(_XRECHNUNG_PROFILE_MARKER in value.lower() for value in ids):
        sample = ids[0].strip()
        msg = (
            f"Not a German E-Rechnung: CustomizationID {sample!r} is not the "
            f"XRechnung CIUS (expected a value containing "
            f"{_XRECHNUNG_PROFILE_MARKER!r})."
        )
        raise ErechnungValidationError(msg)


def _require_cii_german_erechnung_profile(root: etree._Element) -> None:
    ids = root.xpath(
        "rsm:ExchangedDocumentContext/"
        "ram:GuidelineSpecifiedDocumentContextParameter/"
        "ram:ID/text()",
        namespaces=_CII_NSMAP,
    )
    if not ids:
        msg = (
            "Not a German E-Rechnung: GuidelineSpecifiedDocumentContextParameter/ID "
            "is missing. Expected an XRechnung CIUS or a Factur-X EN16931+ profile URN."
        )
        raise ErechnungValidationError(msg)

    declined: list[str] = []
    for value in ids:
        normalized = value.strip().lower()
        if _XRECHNUNG_PROFILE_MARKER in normalized:
            return
        if normalized == _CII_EN16931_COMFORT_PROFILE:
            return
        if any(marker in normalized for marker in _FACTUR_X_EXTENDED_PROFILE_MARKERS):
            return
        declined.append(value.strip())

    sample = declined[0] if declined else "<empty>"
    msg = (
        f"Not a German E-Rechnung: profile URN {sample!r} is not accepted "
        f"(expected XRechnung CIUS or Factur-X/ZUGFeRD EN16931 (COMFORT)/EXTENDED; "
        f"sub-EN16931 profiles like BASIC, BASIC WL and MINIMUM are intentionally "
        f"declined)."
    )
    raise ErechnungValidationError(msg)

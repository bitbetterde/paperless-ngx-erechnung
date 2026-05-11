# SPDX-License-Identifier: GPL-3.0-only
"""Paperless-ngx parser classes for German E-Rechnung.

Two parsers are advertised under the ``paperless_ngx.parsers`` entry-point
group:

- :class:`XRechnungParser` handles pure-XML XRechnung invoices (UBL or CII)
  arriving as ``application/xml`` / ``text/xml``. It renders an archive PDF
  via the KoSIT XRechnung-Visualization XSLT chain.
- :class:`ZUGFeRDParser` handles ZUGFeRD / Factur-X hybrid PDFs. The input
  PDF/A-3 is already the archive — we copy it through unchanged and
  extract metadata from the embedded XML.

Both parsers implement Paperless-ngx's structural
``paperless.parsers.ParserProtocol``.
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Self

from paperless_ngx_erechnung import __version__
from paperless_ngx_erechnung.detection import is_german_erechnung_xml
from paperless_ngx_erechnung.detection import is_xrechnung_xml
from paperless_ngx_erechnung.detection import is_zugferd_pdf
from paperless_ngx_erechnung.extraction import InvoiceData
from paperless_ngx_erechnung.extraction import extract_invoice_fields

if TYPE_CHECKING:
    from types import TracebackType

    from paperless.parsers import MetadataEntry
    from paperless.parsers import ParserContext

logger = logging.getLogger("paperless_ngx_erechnung.parsers")

# Built-in parsers all score 10. Returning 100 cleanly outranks them when
# our detection callbacks confirm a match; returning None when they don't
# means Paperless falls back to the built-in path with no interference.
_WIN_SCORE = 100

_AUTHOR = "Moritz Stückler"
_URL = "https://github.com/bitbetterde/paperless-ngx-erechnung"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _make_tempdir() -> Path:
    """Allocate a temp working directory under SCRATCH_DIR if available.

    Falls back to the system temp directory when Django settings are not
    configured (i.e. when running unit tests outside Paperless-ngx).
    """
    parent: Path | None = None
    try:
        from django.conf import settings  # noqa: PLC0415

        scratch = getattr(settings, "SCRATCH_DIR", None)
        if scratch is not None:
            parent = Path(scratch)
            parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        parent = None

    return Path(tempfile.mkdtemp(prefix="paperless-ngx-erechnung-", dir=parent))


def _searchable_text_block(data: InvoiceData) -> str:
    """Format extracted fields as a Key: Value block for full-text search.

    Keys are ASCII slug-form so that a future Paperless workflow can
    regex-extract them into Custom Fields.
    """
    label_map = {
        "invoice_number": "Invoice-Number",
        "issue_date": "Issue-Date",
        "due_date": "Due-Date",
        "seller_name": "Seller",
        "seller_vat_id": "Seller-VAT-ID",
        "buyer_name": "Buyer",
        "currency": "Currency",
        "total_amount": "Total-Amount",
        "net_amount": "Net-Amount",
        "tax_amount": "Tax-Amount",
    }
    lines = []
    for field, label in label_map.items():
        value = getattr(data, field)
        if value is None:
            continue
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _metadata_entries(data: InvoiceData, namespace: str) -> list[MetadataEntry]:
    """Convert :class:`InvoiceData` into Paperless ``MetadataEntry`` rows."""
    entries: list[MetadataEntry] = []
    for key, value in data.as_dict().items():
        entries.append(
            {  # type: ignore[typeddict-item]
                "namespace": namespace,
                "prefix": "erechnung",
                "key": key,
                "value": value,
            },
        )
    return entries


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: bytes) -> str:
    """Crude tag-strip for the searchable text body.

    We intentionally don't pull in another dep (html2text / BeautifulSoup)
    — the KoSIT HTML output is plain enough that a regex strip plus
    whitespace collapse gives perfectly searchable plain text.
    """
    text = _TAG_RE.sub(" ", html.decode("utf-8", errors="replace"))
    return _WS_RE.sub(" ", text).strip()


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF via pypdfium2; return ``""`` on failure."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover
        return ""

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        pieces: list[str] = []
        for page in pdf:
            text_page = page.get_textpage()
            try:
                pieces.append(text_page.get_text_range() or "")
            finally:
                text_page.close()
        return "\n".join(pieces)
    finally:
        pdf.close()


def _pdf_page_count(pdf_path: Path) -> int | None:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:
        return None


def _date_to_datetime(d: datetime.date | None) -> datetime.datetime | None:
    if d is None:
        return None
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)


def _read_zugferd_embedded_xml(pdf_path: Path) -> bytes | None:
    """Extract the invoice XML embedded in a ZUGFeRD/Factur-X PDF, or None."""
    try:
        import pikepdf
    except ImportError:  # pragma: no cover
        return None

    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            for name in pdf.attachments:
                if name.lower() in {
                    "factur-x.xml",
                    "zugferd-invoice.xml",
                    "xrechnung.xml",
                }:
                    attachment = pdf.attachments[name]
                    file_spec = attachment.get_file()
                    data = bytes(file_spec.read_bytes())
                    if is_german_erechnung_xml(data):
                        return data
    except Exception as exc:
        logger.warning("Failed reading embedded XML from %s: %s", pdf_path, exc)
    return None


# --------------------------------------------------------------------------- #
# XRechnungParser — pure XML
# --------------------------------------------------------------------------- #


_XRECHNUNG_MIME_TYPES: dict[str, str] = {
    "application/xml": ".xml",
    "text/xml": ".xml",
}


class XRechnungParser:
    """Parse pure-XML XRechnung invoices (UBL Invoice/CreditNote or CII)."""

    name: str = "XRechnung"
    version: str = __version__
    author: str = _AUTHOR
    url: str = _URL

    # --- registry contract --------------------------------------------------

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        return _XRECHNUNG_MIME_TYPES

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: Path | None = None,
    ) -> int | None:
        if mime_type not in _XRECHNUNG_MIME_TYPES:
            return None
        # Capability probe: paperless-ngx's is_mime_type_supported() calls this
        # with path=None to ask "can anyone handle this MIME type?". Answer yes
        # so the upload validator accepts the file; content validation runs on
        # the real dispatch call (path is set).
        if path is None:
            return _WIN_SCORE
        if not is_xrechnung_xml(path):
            return None
        return _WIN_SCORE

    # --- properties ---------------------------------------------------------

    @property
    def can_produce_archive(self) -> bool:
        return True

    @property
    def requires_pdf_rendition(self) -> bool:
        # Browsers cannot render an UBL/CII XML for a human; the PDF
        # archive must be present.
        return True

    # --- lifecycle ----------------------------------------------------------

    def __init__(self, logging_group: object = None) -> None:
        self._tempdir = _make_tempdir()
        self._invoice: InvoiceData = InvoiceData()
        self._text: str | None = None
        self._archive_path: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        shutil.rmtree(self._tempdir, ignore_errors=True)

    def configure(self, context: ParserContext) -> None:
        # No per-document configuration needed for XRechnung.
        pass

    # --- parse --------------------------------------------------------------

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        # Defer this import — paperless.parsers may not be importable in
        # unit tests that exercise the plugin in isolation.
        from documents.parsers import ParseError  # noqa: PLC0415

        try:
            xml_bytes = Path(document_path).read_bytes()
        except OSError as exc:
            msg = f"Could not read XRechnung file: {exc}"
            raise ParseError(msg) from exc

        self._invoice = extract_invoice_fields(xml_bytes)

        if produce_archive:
            from paperless_ngx_erechnung.rendering import (  # noqa: PLC0415
                render_xrechnung_to_pdf,
            )

            archive_path = self._tempdir / "archive.pdf"
            try:
                render_xrechnung_to_pdf(xml_bytes, archive_path)
            except Exception as exc:
                msg = f"Failed rendering XRechnung archive PDF: {exc}"
                raise ParseError(msg) from exc
            self._archive_path = archive_path

            # Read the rendered PDF's text for searchability.
            body_text = _extract_pdf_text(archive_path)
        else:
            body_text = ""

        # Prepend the key-value block so it's always at the top of the
        # full-text index.
        kv = _searchable_text_block(self._invoice)
        parts = [p for p in (kv, body_text) if p]
        self._text = "\n\n".join(parts) if parts else None

    # --- accessors ----------------------------------------------------------

    def get_text(self) -> str | None:
        return self._text

    def get_date(self) -> datetime.datetime | None:
        return _date_to_datetime(self._invoice.issue_date)

    def get_archive_path(self) -> Path | None:
        return self._archive_path

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        from paperless_ngx_erechnung.thumbnail import (  # noqa: PLC0415
            render_first_page_webp,
        )

        out = self._tempdir / "thumb.webp"

        # Prefer the rendered archive PDF (already on disk after parse).
        # If get_thumbnail is called before parse, render on demand.
        pdf_for_thumb = self._archive_path
        if pdf_for_thumb is None:
            from paperless_ngx_erechnung.rendering import (  # noqa: PLC0415
                render_xrechnung_to_pdf,
            )

            pdf_for_thumb = self._tempdir / "thumb-source.pdf"
            render_xrechnung_to_pdf(Path(document_path).read_bytes(), pdf_for_thumb)

        return render_first_page_webp(pdf_for_thumb, out)

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        if self._archive_path is not None:
            return _pdf_page_count(self._archive_path)
        return None

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        # When called for the archive copy mime_type is "application/pdf";
        # we still want to surface the same logical metadata, but the
        # original file (document_path) is now the PDF. Re-derive from the
        # cached parse output if possible.
        if any(
            getattr(self._invoice, f) is not None
            for f in (
                "invoice_number",
                "issue_date",
                "seller_name",
                "total_amount",
            )
        ):
            return _metadata_entries(
                self._invoice,
                namespace="urn:paperless-ngx-erechnung:xrechnung",
            )

        # extract_metadata may be invoked without a preceding parse() call
        # (the API view layer reads metadata on demand). Re-parse the XML
        # if we still have it as input.
        if mime_type in _XRECHNUNG_MIME_TYPES:
            try:
                xml_bytes = Path(document_path).read_bytes()
            except OSError:
                return []
            data = extract_invoice_fields(xml_bytes)
            return _metadata_entries(
                data,
                namespace="urn:paperless-ngx-erechnung:xrechnung",
            )

        return []


# --------------------------------------------------------------------------- #
# ZUGFeRDParser — hybrid PDF
# --------------------------------------------------------------------------- #


_ZUGFERD_MIME_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
}


class ZUGFeRDParser:
    """Parse ZUGFeRD / Factur-X hybrid PDFs.

    The input PDF/A-3 already is the archive. We extract the embedded
    invoice XML for metadata and full-text search; the visual rendition
    comes for free from the original PDF.
    """

    name: str = "ZUGFeRD / Factur-X"
    version: str = __version__
    author: str = _AUTHOR
    url: str = _URL

    # --- registry contract --------------------------------------------------

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        return _ZUGFERD_MIME_TYPES

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: Path | None = None,
    ) -> int | None:
        if mime_type not in _ZUGFERD_MIME_TYPES:
            return None
        # See XRechnungParser.score for the path-None rationale.
        if path is None:
            return _WIN_SCORE
        if not is_zugferd_pdf(path):
            return None
        return _WIN_SCORE

    # --- properties ---------------------------------------------------------

    @property
    def can_produce_archive(self) -> bool:
        return True

    @property
    def requires_pdf_rendition(self) -> bool:
        # Source is already a PDF the browser can render.
        return False

    # --- lifecycle ----------------------------------------------------------

    def __init__(self, logging_group: object = None) -> None:
        self._tempdir = _make_tempdir()
        self._invoice: InvoiceData = InvoiceData()
        self._text: str | None = None
        self._archive_path: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        shutil.rmtree(self._tempdir, ignore_errors=True)

    def configure(self, context: ParserContext) -> None:
        pass

    # --- parse --------------------------------------------------------------

    def parse(
        self,
        document_path: Path,
        mime_type: str,
        *,
        produce_archive: bool = True,
    ) -> None:
        from documents.parsers import ParseError  # noqa: PLC0415

        document_path = Path(document_path)
        if not document_path.is_file():
            msg = f"ZUGFeRD source file not found: {document_path}"
            raise ParseError(msg)

        # Extract embedded XML and build InvoiceData from it.
        xml_bytes = _read_zugferd_embedded_xml(document_path)
        if xml_bytes is None:
            logger.warning(
                "No embedded XML found in %s; falling back to PDF text only.",
                document_path,
            )
        else:
            self._invoice = extract_invoice_fields(xml_bytes)

        if produce_archive:
            # Pass-through: copy the input verbatim so the Factur-X
            # signature and PDF/A-3 conformance are preserved.
            archive_path = self._tempdir / "archive.pdf"
            shutil.copyfile(document_path, archive_path)
            self._archive_path = archive_path

        body_text = _extract_pdf_text(document_path)
        kv = _searchable_text_block(self._invoice)
        parts = [p for p in (kv, body_text) if p]
        self._text = "\n\n".join(parts) if parts else None

    # --- accessors ----------------------------------------------------------

    def get_text(self) -> str | None:
        return self._text

    def get_date(self) -> datetime.datetime | None:
        return _date_to_datetime(self._invoice.issue_date)

    def get_archive_path(self) -> Path | None:
        return self._archive_path

    def get_thumbnail(self, document_path: Path, mime_type: str) -> Path:
        from paperless_ngx_erechnung.thumbnail import (  # noqa: PLC0415
            render_first_page_webp,
        )

        out = self._tempdir / "thumb.webp"
        return render_first_page_webp(Path(document_path), out)

    def get_page_count(self, document_path: Path, mime_type: str) -> int | None:
        return _pdf_page_count(Path(document_path))

    def extract_metadata(
        self,
        document_path: Path,
        mime_type: str,
    ) -> list[MetadataEntry]:
        if any(
            getattr(self._invoice, f) is not None
            for f in (
                "invoice_number",
                "issue_date",
                "seller_name",
                "total_amount",
            )
        ):
            return _metadata_entries(
                self._invoice,
                namespace="urn:paperless-ngx-erechnung:zugferd",
            )

        # On-demand path (API view layer reading metadata without parse).
        xml_bytes = _read_zugferd_embedded_xml(Path(document_path))
        if xml_bytes is None:
            return []
        data = extract_invoice_fields(xml_bytes)
        return _metadata_entries(
            data,
            namespace="urn:paperless-ngx-erechnung:zugferd",
        )

# SPDX-License-Identifier: GPL-3.0-only
"""Generate WebP thumbnails from PDF files via pypdfium2.

Mirrors the strategy used in Paperless-ngx's built-in
``RasterisedDocumentParser`` — render the first page of the PDF and save it
as a WebP image, the format Paperless expects for thumbnails.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("paperless_erechnung.thumbnail")


def render_first_page_webp(
    pdf_path: Path,
    out_path: Path,
    *,
    scale: float = 1.5,
) -> Path:
    """Render the first page of *pdf_path* to a WebP at *out_path*.

    Returns the output path. *scale* roughly tracks DPI relative to the
    72-pt PDF baseline (1.5 ≈ 108 DPI, good enough for the sidebar).
    """
    # Lazy import: pypdfium2 has C-extension load cost.
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(pdf) == 0:
            msg = f"PDF {pdf_path} has no pages to thumbnail."
            raise RuntimeError(msg)
        bitmap = pdf[0].render(scale=scale)
        image = bitmap.to_pil()
    finally:
        pdf.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="WEBP")
    return out_path

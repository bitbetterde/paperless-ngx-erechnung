# SPDX-License-Identifier: GPL-3.0-only
"""Score-conflict tests: confirm our parsers don't poach files they shouldn't.

These are the most important tests for a third-party plugin — a buggy
``score()`` callback could starve Paperless's built-in parsers of every
PDF or XML file on the system.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperless_erechnung.parsers import XRechnungParser
from paperless_erechnung.parsers import ZUGFeRDParser


# --------------------------------------------------------------------------- #
# Plain (non-invoice) PDFs / XMLs must be declined.
# --------------------------------------------------------------------------- #


def test_xrechnung_declines_random_xml(tmp_path: Path) -> None:
    f = tmp_path / "config.xml"
    f.write_text("<config><logger level='INFO'/></config>")
    assert XRechnungParser.score("application/xml", "config.xml", f) is None


def test_zugferd_declines_plain_pdf(tmp_path: Path) -> None:
    pytest.importorskip("pikepdf")
    import pikepdf  # noqa: PLC0415

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    out = tmp_path / "scan.pdf"
    pdf.save(str(out))
    assert ZUGFeRDParser.score("application/pdf", "scan.pdf", out) is None


def test_xrechnung_score_high_enough_to_outrank_builtins(
    ubl_invoice_path: Path,
) -> None:
    # Paperless-ngx's built-in parsers all score 10
    # (verified in src/paperless/parsers/*.py).
    builtin_score = 10
    score = XRechnungParser.score(
        "application/xml",
        "invoice.xml",
        ubl_invoice_path,
    )
    assert score is not None and score > builtin_score


def test_zugferd_score_high_enough_to_outrank_builtins(
    tmp_path: Path,
    cii_invoice_bytes: bytes,
) -> None:
    pytest.importorskip("pikepdf")
    import pikepdf  # noqa: PLC0415

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

    score = ZUGFeRDParser.score("application/pdf", "z.pdf", out)
    assert score is not None and score > 10

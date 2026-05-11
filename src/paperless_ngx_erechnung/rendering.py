# SPDX-License-Identifier: GPL-3.0-only
"""Render XRechnung XML to an archive PDF.

Pipeline:

1. **Normalize** — KoSIT XSLT (``ubl-invoice-xr.xsl`` /
   ``ubl-creditnote-xr.xsl`` / ``cii-xr.xsl``) collapses the two source
   syntaxes into KoSIT's intermediate "XR" XML.
2. **Render** — ``xrechnung-html.xsl`` produces HTML, which we then
   post-process with a small print stylesheet (see ``_PRINT_STYLESHEET``).
3. **PDF** — WeasyPrint converts that HTML to a PDF.

Both XSLT stages need an XSLT 2.0 processor; we use ``saxonche`` (Saxon-HE
bundled as a native binary, no JRE). The processor is created lazily so
import-time cost stays cheap when the parser isn't actually used.

Why HTML, not KoSIT's PDF stylesheet?
-------------------------------------
KoSIT ships a second renderer, ``xr-pdf.xsl``, that goes straight from XR
to PDF. Tempting — but it emits **XSL-FO**, the W3C formatting-objects
language, and is meant to be processed by **Apache FOP** or Antenna House
XSL Formatter. Both are JVM applications. Adopting the FO path would mean:

- shipping a JRE inside the Paperless-ngx container (no Java today —
  ``saxonche`` is a native Saxon build that does not need a JVM),
- adding ~80 MB to the image and a long-running FOP process for every
  consumed invoice, or wrapping the Antenna House commercial formatter,
- maintaining a second rendering toolchain alongside the HTML viewer.

The HTML viewer is the path most other consumer-side tools use and is what
KoSIT publishes for browser display. The catch: the viewer is *interactive*
— it has a tab navigation that requires JavaScript and four of five tab
panels start hidden (``class="divHide"`` with ``display: none``). Each
hidden panel also contains a ``<noscript>`` block reading
"Inhalte auf dieser Seite sind ohne JavaScript nur eingeschränkt
darstellbar." WeasyPrint never executes JS, so a naïve HTML→PDF conversion
gives a PDF with dead tab buttons and the JS-required warning in place of
4/5 of the invoice.

We solve that by injecting a tiny print-mode stylesheet at the WeasyPrint
stage (see ``_PRINT_STYLESHEET``) that:

- hides the ``.menue`` nav, ``<noscript>`` and ``<script>`` elements,
- forces ``.divHide`` to ``display: block`` so all sections print,
- sets A4 page geometry.

Result: zero extra runtime dependencies, the whole invoice prints, and we
keep using the renderer that gets the most upstream attention.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

logger = logging.getLogger("paperless_ngx_erechnung.rendering")


_XSLT_DIR = Path(__file__).parent / "xslt"

# Source-syntax -> normalizer stylesheet (stage 1).
_NORMALIZER_FOR_ROOT: dict[str, str] = {
    "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice": (
        "ubl-invoice-xr.xsl"
    ),
    "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote": (
        "ubl-creditnote-xr.xsl"
    ),
    "{urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100}"
    "CrossIndustryInvoice": "cii-xr.xsl",
}

# Stage 2 stylesheet (XR -> HTML).
_HTML_STYLESHEET = "xrechnung-html.xsl"


class RenderingNotConfiguredError(RuntimeError):
    """Raised when the vendored KoSIT XSLT stylesheets are missing.

    The package ships with an empty ``xslt/`` directory by design — see
    ``xslt/README.md`` for how to vendor the upstream release before
    cutting a plugin release.
    """


def render_xrechnung_to_pdf(xml_bytes: bytes, out_pdf: Path) -> None:
    """Render *xml_bytes* (an XRechnung document) to a PDF at *out_pdf*.

    Raises
    ------
    RenderingNotConfiguredError
        If the KoSIT XSLT files have not been vendored.
    RuntimeError
        If any stage of the transform fails.
    """
    stylesheet = _select_normalizer(xml_bytes)
    _ensure_xslt_present(stylesheet)
    _ensure_xslt_present(_HTML_STYLESHEET)

    html = _xml_to_html(xml_bytes, stylesheet)
    _html_to_pdf(html, out_pdf)


# --------------------------------------------------------------------------- #
# Stage 1 + 2: XSLT via saxonche
# --------------------------------------------------------------------------- #


def _select_normalizer(xml_bytes: bytes) -> str:
    """Return the stage-1 stylesheet filename for the given XML's root."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        msg = f"Could not parse invoice XML for rendering: {exc}"
        raise RuntimeError(msg) from exc

    stylesheet = _NORMALIZER_FOR_ROOT.get(root.tag)
    if stylesheet is None:
        msg = f"No KoSIT normalizer registered for root element {root.tag!r}"
        raise RuntimeError(msg)
    return stylesheet


def _ensure_xslt_present(filename: str) -> None:
    if not (_XSLT_DIR / filename).is_file():
        msg = (
            f"KoSIT stylesheet {filename!r} not found in {_XSLT_DIR}. "
            "Vendor the upstream release first — see xslt/README.md."
        )
        raise RenderingNotConfiguredError(msg)


def _xml_to_html(xml_bytes: bytes, normalizer_filename: str) -> bytes:
    """Run the two XSLT 2.0 stages: input XML -> XR -> HTML.

    ``saxonche.transform_to_string`` accepts a ``source_file`` path rather
    than an in-memory string, so each stage's input is staged through a
    temp file. The files are cleaned up via the surrounding TemporaryDirectory.
    """
    # Import lazily — saxonche pulls a ~40 MB native binary at first import.
    import tempfile

    from saxonche import PySaxonProcessor

    normalizer_path = str(_XSLT_DIR / normalizer_filename)
    html_path = str(_XSLT_DIR / _HTML_STYLESHEET)

    with tempfile.TemporaryDirectory(prefix="paperless-ngx-erechnung-render-") as tmp:
        src_path = Path(tmp) / "input.xml"
        src_path.write_bytes(xml_bytes)

        with PySaxonProcessor(license=False) as proc:
            xslt = proc.new_xslt30_processor()

            # Stage 1: source XML -> XR
            stage1 = xslt.transform_to_string(
                source_file=str(src_path),
                stylesheet_file=normalizer_path,
            )
            if stage1 is None:
                msg = "KoSIT normalizer stage produced no output."
                raise RuntimeError(msg)

            xr_path = Path(tmp) / "xr.xml"
            xr_path.write_text(stage1, encoding="utf-8")

            # Stage 2: XR -> HTML
            stage2 = xslt.transform_to_string(
                source_file=str(xr_path),
                stylesheet_file=html_path,
            )
            if stage2 is None:
                msg = "KoSIT HTML stage produced no output."
                raise RuntimeError(msg)

            return stage2.encode("utf-8")


# --------------------------------------------------------------------------- #
# Stage 3: HTML -> PDF via WeasyPrint
# --------------------------------------------------------------------------- #


# KoSIT's xrechnung-html.xsl produces a browser-oriented *viewer*:
#   - a tab navigation that needs JavaScript to switch panels;
#   - 4 of 5 sections start with `class="divHide"` (display:none) so without
#     JS only the overview is visible;
#   - a `<noscript>` block inside each section that reads "Inhalte auf dieser
#     Seite sind ohne JavaScript nur eingeschränkt darstellbar."
# WeasyPrint never runs JS, so naïvely converting that HTML gives a PDF
# containing dead tab buttons and the JS-warning text. The print sheet below
# (a) hides the interactive chrome, (b) unfolds all tab panels so the full
# invoice prints, and (c) gives the page sane A4 margins.
_PRINT_STYLESHEET = """
@page { size: A4; margin: 1.8cm 1.5cm; }

/* Hide the interactive viewer chrome — irrelevant in a print rendition. */
.menue, .menue *,
noscript,
script { display: none !important; }

/* Unfold all tab panels so the whole document prints, not just the overview. */
.divHide { display: block !important; }

/* Keep section headers with their content rather than orphaning them. */
h1, h2, h3, h4 { page-break-after: avoid; }
"""


def _html_to_pdf(html: bytes, out_pdf: Path) -> None:
    """Write *html* to *out_pdf* as a PDF using WeasyPrint."""
    # Lazy import — WeasyPrint imports pango/cairo bindings at module load.
    from weasyprint import CSS, HTML

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html.decode("utf-8")).write_pdf(
        target=str(out_pdf),
        stylesheets=[CSS(string=_PRINT_STYLESHEET)],
    )

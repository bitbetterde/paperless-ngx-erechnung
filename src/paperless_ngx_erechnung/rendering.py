# SPDX-License-Identifier: GPL-3.0-only
"""Render XRechnung XML to an archive PDF.

Pipeline:

1. **Normalize** — KoSIT XSLT (``ubl-invoice-xr.xsl`` /
   ``ubl-creditnote-xr.xsl`` / ``cii-xr.xsl``) collapses the two source
   syntaxes into KoSIT's intermediate "XR" XML.
2. **Render** — ``xr-pdf.xsl`` produces XSL-FO (Apache FOP / Antenna House
   formatting objects), KoSIT's canonical PDF stylesheet.
3. **PDF** — Apache FOP turns the XSL-FO into a PDF.

The XSLT stages need an XSLT 2.0 processor; we use ``saxonche`` (Saxon-HE
bundled as a native binary, no JRE). FOP itself is a JVM application; we
shell out to the ``fop`` binary on PATH, which the Docker image
installs alongside ``default-jre-headless``.

Why FOP and not WeasyPrint (HTML route)?
----------------------------------------
KoSIT also ships ``xrechnung-html.xsl``, which targets an interactive
browser viewer (tab navigation, JavaScript-driven panels). We tried that
route with WeasyPrint and the resulting archive PDF was a degraded view
of the invoice — viewer chrome leaked through, panels were hidden, the
print stylesheet was an ongoing maintenance burden, and there is no path
to PDF/A from a raster-leaning HTML renderer. ``xr-pdf.xsl`` is purpose-built
for archive PDFs, ships from KoSIT, and is the stylesheet that produces
the layout the spec authors actually intended.

The price is a JRE in the container image. We pay it.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
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

# Stage 2 stylesheet (XR -> XSL-FO).
_PDF_STYLESHEET = "xr-pdf.xsl"

# How long FOP may run before we give up — KoSIT's FO for a typical invoice
# is ~130 KB; 120s is comfortably generous on the modest CPUs Paperless
# typically runs on and short enough to fail fast on a hang.
_FOP_TIMEOUT_SECONDS = 120


class RenderingNotConfiguredError(RuntimeError):
    """Raised when the vendored KoSIT XSLT stylesheets are missing.

    The package ships with an empty ``xslt/`` directory by design — see
    ``xslt/README.md`` for how to vendor the upstream release before
    cutting a plugin release.
    """


class FopNotAvailableError(RuntimeError):
    """Raised when the ``fop`` binary cannot be found on PATH.

    Apache FOP is an external dependency installed by the Docker image
    (``default-jre-headless`` + ``fop``). Local development needs it on
    PATH as well — see the README's Development section.
    """


def render_xrechnung_to_pdf(xml_bytes: bytes, out_pdf: Path) -> None:
    """Render *xml_bytes* (an XRechnung document) to a PDF at *out_pdf*.

    Raises
    ------
    RenderingNotConfiguredError
        If the KoSIT XSLT files have not been vendored.
    FopNotAvailableError
        If the ``fop`` binary is not on PATH.
    RuntimeError
        If any stage of the transform fails.
    """
    stylesheet = _select_normalizer(xml_bytes)
    _ensure_xslt_present(stylesheet)
    _ensure_xslt_present(_PDF_STYLESHEET)

    fo_bytes = _xml_to_fo(xml_bytes, stylesheet)
    _fo_to_pdf(fo_bytes, out_pdf)


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


def _xml_to_fo(xml_bytes: bytes, normalizer_filename: str) -> bytes:
    """Run the two XSLT 2.0 stages: input XML -> XR -> XSL-FO.

    ``saxonche.transform_to_string`` accepts a ``source_file`` path rather
    than an in-memory string, so each stage's input is staged through a
    temp file. The files are cleaned up via the surrounding TemporaryDirectory.
    """
    # Import lazily — saxonche pulls a ~40 MB native binary at first import.
    from saxonche import PySaxonProcessor

    normalizer_path = str(_XSLT_DIR / normalizer_filename)
    pdf_xsl_path = str(_XSLT_DIR / _PDF_STYLESHEET)

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

            # Stage 2: XR -> XSL-FO
            stage2 = xslt.transform_to_string(
                source_file=str(xr_path),
                stylesheet_file=pdf_xsl_path,
            )
            if stage2 is None:
                msg = "KoSIT PDF stage produced no XSL-FO output."
                raise RuntimeError(msg)

            return stage2.encode("utf-8")


# --------------------------------------------------------------------------- #
# Stage 3: XSL-FO -> PDF via Apache FOP
# --------------------------------------------------------------------------- #


def _fo_to_pdf(fo_bytes: bytes, out_pdf: Path) -> None:
    """Write *fo_bytes* (XSL-FO) to *out_pdf* as a PDF using Apache FOP.

    Shells out to the ``fop`` binary, which the Docker image installs
    alongside a headless JRE. FOP reads the FO from a temp file (its CLI
    does support ``-fo -`` for stdin in newer releases, but Debian's ``fop``
    package still ships a wrapper that doesn't, so a tempfile is the
    cross-platform path).
    """
    fop_bin = shutil.which("fop")
    if fop_bin is None:
        msg = (
            "Apache FOP binary not found on PATH. In the Docker image, "
            "ensure the `fop` package is installed (the Dockerfile adds it "
            "via apt). For local development, install it via your package "
            "manager: `brew install fop` on macOS, `apt install fop` on "
            "Debian/Ubuntu."
        )
        raise FopNotAvailableError(msg)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="paperless-ngx-erechnung-fop-") as tmp:
        fo_path = Path(tmp) / "input.fo"
        fo_path.write_bytes(fo_bytes)

        try:
            proc = subprocess.run(
                [fop_bin, "-fo", str(fo_path), "-pdf", str(out_pdf)],
                capture_output=True,
                timeout=_FOP_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"Apache FOP timed out after {_FOP_TIMEOUT_SECONDS}s rendering XSL-FO to PDF."
            raise RuntimeError(msg) from exc

        if proc.returncode != 0 or not out_pdf.is_file():
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            stdout = proc.stdout.decode("utf-8", errors="replace").strip()
            detail = stderr or stdout or "no diagnostic output"
            msg = (
                f"Apache FOP failed (exit {proc.returncode}) rendering XSL-FO "
                f"to PDF: {detail}"
            )
            raise RuntimeError(msg)

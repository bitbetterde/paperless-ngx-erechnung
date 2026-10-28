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
bundled as a native binary, no JRE). FOP itself is a JVM application and
runs in one of two ways:

* If ``PAPERLESS_ERECHNUNG_FOP_URL`` is set, the FO is POSTed to the FOP
  service in ``fop-server/`` (a separate container), so the Paperless
  image needs no JRE.
* Otherwise we shell out to the ``fop`` binary on PATH.

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

The price is a JRE somewhere: in the Paperless image, or in the FOP
service container.
"""

from __future__ import annotations

import logging
import shutil
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from lxml import etree

logger = logging.getLogger("paperless_ngx_erechnung.rendering")


_XSLT_DIR = Path(__file__).parent / "xslt"

# FOP configuration registering the vendored Source Serif Pro font that the
# KoSIT stylesheet names via font-family="SourceSerifPro". Without `-c`, FOP
# silently substitutes a base-14 fallback (Times-Roman).
_FOP_CONFIG = _XSLT_DIR / "conf" / "fop.xconf"

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

# Base URL of the FOP service (e.g. ``http://fop:8080``). When set, FOP runs
# remotely and the local ``fop`` binary is not needed.
FOP_URL_ENV = "PAPERLESS_ERECHNUNG_FOP_URL"

_INSTALL_DOCS_URL = (
    "https://github.com/bitbetterde/paperless-ngx-erechnung/blob/main/INSTALL.md"
)

# The FOP service's error responses are short text; cap what we read.
_MAX_ERROR_DETAIL_BYTES = 4096


class RenderingNotConfiguredError(RuntimeError):
    """Raised when the vendored KoSIT XSLT stylesheets are missing.

    The package ships with an empty ``xslt/`` directory by design — see
    ``xslt/README.md`` for how to vendor the upstream release before
    cutting a plugin release.
    """


class FopNotAvailableError(RuntimeError):
    """Raised when no FOP service is configured and ``fop`` is not on PATH.

    Apache FOP is an external dependency: either the FOP service container
    (``PAPERLESS_ERECHNUNG_FOP_URL``) or a local ``fop`` binary plus a JRE.
    See INSTALL.md.
    """


def render_xrechnung_to_pdf(xml_bytes: bytes, out_pdf: Path) -> None:
    """Render *xml_bytes* (an XRechnung document) to a PDF at *out_pdf*.

    Raises
    ------
    RenderingNotConfiguredError
        If the KoSIT XSLT files have not been vendored.
    FopNotAvailableError
        If no FOP service is configured and ``fop`` is not on PATH.
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


def normalize_to_xr(xml_bytes: bytes) -> bytes:
    """Run only stage 1: UBL / CII source XML -> KoSIT's intermediate XR XML.

    In XR every value is an element tagged with its EN 16931 business term
    (``xr:id="BT-22"``) and the XPath of the source node it came from
    (``xr:src``). No FOP needed.

    Raises
    ------
    RenderingNotConfiguredError
        If the KoSIT XSLT files have not been vendored.
    RuntimeError
        If the root element is unknown or the transform fails.
    """
    stylesheet = _select_normalizer(xml_bytes)
    _ensure_xslt_present(stylesheet)
    return _xml_to_xr(xml_bytes, stylesheet)


def _transform(source: bytes, stylesheet_filename: str, tmp: Path) -> str | None:
    """Apply one vendored stylesheet to *source* via saxonche.

    ``saxonche.transform_to_string`` accepts a ``source_file`` path rather
    than an in-memory string, so the input is staged through a file in *tmp*.
    """
    # Import lazily — saxonche pulls a ~40 MB native binary at first import.
    from saxonche import PySaxonProcessor

    src_path = tmp / f"{stylesheet_filename}.input.xml"
    src_path.write_bytes(source)
    with PySaxonProcessor(license=False) as proc:
        return proc.new_xslt30_processor().transform_to_string(
            source_file=str(src_path),
            stylesheet_file=str(_XSLT_DIR / stylesheet_filename),
        )


def _xml_to_xr(xml_bytes: bytes, normalizer_filename: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="paperless-ngx-erechnung-xr-") as tmp:
        xr = _transform(xml_bytes, normalizer_filename, Path(tmp))
    if xr is None:
        msg = "KoSIT normalizer stage produced no output."
        raise RuntimeError(msg)
    return xr.encode("utf-8")


def _xml_to_fo(xml_bytes: bytes, normalizer_filename: str) -> bytes:
    """Run the two XSLT 2.0 stages: input XML -> XR -> XSL-FO."""
    xr = _xml_to_xr(xml_bytes, normalizer_filename)
    with tempfile.TemporaryDirectory(prefix="paperless-ngx-erechnung-render-") as tmp:
        fo = _transform(xr, _PDF_STYLESHEET, Path(tmp))
    if fo is None:
        msg = "KoSIT PDF stage produced no XSL-FO output."
        raise RuntimeError(msg)
    return fo.encode("utf-8")


# --------------------------------------------------------------------------- #
# Stage 3: XSL-FO -> PDF via Apache FOP
# --------------------------------------------------------------------------- #


def _materialize_fop_config(tmp_dir: Path) -> Path | None:
    """Stage a FOP config in *tmp_dir* with absolute font ``embed-url`` paths.

    FOP resolves relative ``embed-url`` URIs against the input FO file's
    directory, not the config file's. Since we write the FO into a tempdir,
    the upstream ``fonts/...`` paths would fail to resolve. We rewrite them
    to absolute ``file:`` URIs pointing at the vendored TTFs. Returns None
    if the vendored config is missing (FOP then runs unconfigured and
    silently substitutes a base-14 fallback for SourceSerifPro).
    """
    if not _FOP_CONFIG.is_file():
        return None
    fonts_dir = (_FOP_CONFIG.parent / "fonts").resolve()
    text = _FOP_CONFIG.read_text(encoding="utf-8")
    rewritten = text.replace('embed-url="fonts/', f'embed-url="{fonts_dir.as_uri()}/')
    staged = tmp_dir / "fop.xconf"
    staged.write_text(rewritten, encoding="utf-8")
    return staged


def _fo_to_pdf(fo_bytes: bytes, out_pdf: Path) -> None:
    """Write *fo_bytes* (XSL-FO) to *out_pdf* as a PDF using Apache FOP.

    Uses the FOP service when ``PAPERLESS_ERECHNUNG_FOP_URL`` is set,
    otherwise the local ``fop`` binary.
    """
    service_url = os.environ.get(FOP_URL_ENV, "").strip()
    if service_url:
        _fo_to_pdf_remote(fo_bytes, out_pdf, service_url)
    else:
        _fo_to_pdf_local(fo_bytes, out_pdf)


def _fo_to_pdf_remote(fo_bytes: bytes, out_pdf: Path, service_url: str) -> None:
    """Render via the FOP service: POST the FO to ``<service_url>/render``.

    Proxy environment variables are ignored on purpose: the service is
    meant to sit next to Paperless on an internal network, and routing
    that traffic through a corporate proxy would break it.
    """
    endpoint = service_url.rstrip("/") + "/render"
    if urllib.parse.urlsplit(endpoint).scheme not in ("http", "https"):
        msg = f"{FOP_URL_ENV} must be an http:// or https:// URL, got {service_url!r}."
        raise RuntimeError(msg)

    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        endpoint,
        data=fo_bytes,
        method="POST",
        headers={"Content-Type": "application/xml"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=_FOP_TIMEOUT_SECONDS) as response:
            pdf_bytes = response.read()
    except urllib.error.HTTPError as exc:
        body = (
            exc.read(_MAX_ERROR_DETAIL_BYTES).decode("utf-8", errors="replace").strip()
        )
        detail = body or str(exc.reason)
        msg = (
            f"FOP service at {endpoint} failed (HTTP {exc.code}) rendering "
            f"XSL-FO to PDF: {detail}"
        )
        raise RuntimeError(msg) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        msg = f"Could not reach FOP service at {endpoint}: {reason}"
        raise RuntimeError(msg) from exc

    if not pdf_bytes.startswith(b"%PDF-"):
        msg = f"FOP service at {endpoint} returned a response that is not a PDF."
        raise RuntimeError(msg)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    out_pdf.write_bytes(pdf_bytes)


def _fo_to_pdf_local(fo_bytes: bytes, out_pdf: Path) -> None:
    """Render with the local ``fop`` binary.

    Shells out to the ``fop`` binary, which the Docker image installs
    alongside a headless JRE. FOP reads the FO from a temp file (its CLI
    does support ``-fo -`` for stdin in newer releases, but Debian's ``fop``
    package still ships a wrapper that doesn't, so a tempfile is the
    cross-platform path).
    """
    fop_bin = shutil.which("fop")
    if fop_bin is None:
        msg = (
            f"Apache FOP not available: {FOP_URL_ENV} is not set and the "
            "`fop` binary is not on PATH. In Docker, run the FOP service "
            f"container and set {FOP_URL_ENV} (see {_INSTALL_DOCS_URL}), or install "
            "the `fop` package in your image. For local development, install "
            "it via your package manager: `brew install fop` on macOS, "
            "`apt install fop` on Debian/Ubuntu."
        )
        raise FopNotAvailableError(msg)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="paperless-ngx-erechnung-fop-") as tmp:
        fo_path = Path(tmp) / "input.fo"
        fo_path.write_bytes(fo_bytes)

        cmd = [fop_bin]
        runtime_config = _materialize_fop_config(Path(tmp))
        if runtime_config is not None:
            cmd += ["-c", str(runtime_config)]
        cmd += ["-fo", str(fo_path), "-pdf", str(out_pdf)]

        try:
            proc = subprocess.run(
                cmd,
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

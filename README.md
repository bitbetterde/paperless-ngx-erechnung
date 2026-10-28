# paperless-ngx-erechnung

German E-Rechnung parser plugin for [Paperless-ngx v3](https://github.com/paperless-ngx/paperless-ngx).

Handles the two formats produced by the German B2B E-Rechnung mandate and makes their data visible and searchable in Paperless-ngx:

- **XRechnung** — pure XML invoice (UBL Invoice, UBL CreditNote, or UN/CEFACT CII).
- **ZUGFeRD / Factur-X** — hybrid PDF/A-3 with an embedded XML invoice.

![Paperless-ngx showing an XRechnung invoice with extracted metadata in the sidebar and the rendered archive PDF in the main view](paperless-ngx-erechnung.png)

## How it plugs in

Paperless-ngx exposes a parser plugin framework that scans the `paperless_ngx.parsers` Python entry-point group on startup (see `paperless/parsers/registry.py` in paperless-ngx). This package declares two parsers under that group:

| Parser             | MIME types                          | Score |
|--------------------|-------------------------------------|-------|
| `XRechnungParser`  | `application/xml`, `text/xml`       | 100   |
| `ZUGFeRDParser`    | `application/pdf`                   | 100   |

Both parsers gate their `score()` on content inspection — XRechnung confirms the root element namespace, ZUGFeRD looks for an embedded `factur-x.xml` / `ZUGFeRD-invoice.xml`. When the file does **not** match, `score()` returns `None` and Paperless's built-in parsers take over normally.

Built-in parsers score `10`; this plugin scores `100`, so it cleanly outranks defaults for matching files without affecting anything else.

## What this plugin does

- Archive PDF rendering for XRechnung via the official KoSIT [XRechnung-Visualization](https://github.com/itplr-kosit/xrechnung-visualization) XSLT (`xr-pdf.xsl`) plus Apache FOP (XSL-FO → PDF).
- Pass-through archive for ZUGFeRD PDFs (preserves PDF/A-3 conformance and the Factur-X signature).
- Extracted invoice fields (number, date, due date, seller, totals, …) surfaced in Paperless's metadata sidebar and prepended to the searchable text body.
- Every field of the invoice XML listed in the Content tab and full-text index, labelled with its EN 16931 business term ID (e.g. `BT-22 Invoice note (Bemerkung): …`), so fields can be found by their BT number. XML fields without a BT number (e.g. Factur-X EXTENDED extras) follow by their XML path.

## What this plugin does NOT do

- Creating Custom Fields (no plugin hook for this exists yet) from XML invoice fields
- Generation / creation of E-Rechnungen (Paperless-ngx is not a data entry tool)
- OCR fallback for scanned/non-Factur-X PDFs — those continue to use Paperless's built-in Tesseract path.

## Vendored KoSIT assets

`src/paperless_ngx_erechnung/xslt/` contains a pinned snapshot of the [KoSIT XRechnung-Visualization project](https://projekte.kosit.org/xrechnung/xrechnung-visualization) (Apache-2.0): the XSLT stylesheets, the Apache FOP configuration (`conf/fop.xconf`), and the Source Serif Pro TTF fonts (`conf/fonts/`, SIL OFL) that the FOP config registers. See `xslt/README.md` for the pinned version and re-vendoring instructions.

## Installation

The plugin needs Apache FOP (a Java application) to render XRechnung archive PDFs. For Docker, the recommended setup keeps the official Paperless-ngx image unchanged: FOP runs in its own container, and the plugin is installed by a startup script or mounted as a folder.

See [INSTALL.md](https://github.com/bitbetterde/paperless-ngx-erechnung/blob/main/INSTALL.md) for step-by-step instructions for Docker Compose, a custom Docker image and bare metal.

## Development

Archive PDF rendering needs **Apache FOP**. For local development, either install it via your package manager:

```bash
brew install fop          # macOS — pulls in openjdk
apt install fop           # Debian/Ubuntu — pulls in default-jre-headless
```

or run the FOP service container and point the plugin at it:

```bash
docker build -f fop-server/Dockerfile -t paperless-ngx-erechnung-fop .
docker run -d -p 127.0.0.1:8080:8080 paperless-ngx-erechnung-fop
export PAPERLESS_ERECHNUNG_FOP_URL=http://localhost:8080
```

The XSLT-dependent rendering tests skip if neither is available. All other runtime dependencies (qpdf for pikepdf, libxml2/libxslt for lxml, libpdfium, Saxon-HE native libs, Pillow) are bundled in their respective wheels and need no system install.

Then:

```bash
uv sync
uv run pytest
```

## AI Disclaimer

This plugin was developed with the help of an AI Agent (Claude Code). However, it was not "vibecoded" (as in "a human did not look at or understand this code") or "one-shotted". Every line of this code was reviewed by a capable developer and the whole project is tested, refined and documented by real humans.

## License

GPL-3.0-only — matching Paperless-ngx itself, since this plugin runs in-process with Paperless and the combined work is most cleanly distributed under a single license. See `LICENSE` for the full text.

Bundled KoSIT XSLT under `src/paperless_ngx_erechnung/xslt/` is Apache-2.0 (GPL-3.0-compatible); see `src/paperless_ngx_erechnung/xslt/LICENSE`.

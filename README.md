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

Unfortunately, installing this plugin is more than a single click and depends very much on your setup. Two pieces need to be in place:

1. The Python package itself, installed into the same interpreter Paperless runs under, so its `paperless_ngx.parsers` entry-points are discovered at startup.
2. **Apache FOP**, which turns XRechnung invoices into archive PDFs. FOP is a Java application. It can run as a separate container next to Paperless (recommended for Docker), or as a local `fop` binary with a Java runtime.

Once both are in place, Paperless picks up the parsers at startup — look for `Loaded third-party parser 'XRechnung' …` in the logs to confirm successful installation.

### Docker / Docker Compose (recommended)

This setup keeps the official Paperless-ngx image unchanged: Paperless installs the plugin with a startup script, and FOP runs in its own container (`ghcr.io/bitbetterde/paperless-ngx-erechnung-fop`).

1. Set up Paperless-ngx with Docker Compose (e.g. by using [one of the official compose files](https://github.com/paperless-ngx/paperless-ngx/tree/main/docker/compose)).
2. Next to your `docker-compose.yml`, create a folder `custom-init` with a script `install-erechnung.sh`:

```bash
#!/bin/bash
pip install --no-cache-dir paperless-ngx-erechnung
```

   Paperless only runs startup scripts owned by `root` and not writable by others:

```bash
sudo chown -R root:root custom-init
sudo chmod 755 custom-init custom-init/install-erechnung.sh
```

3. Add the FOP service, mount the script folder and point Paperless at the FOP service in your `docker-compose.yml`:

```yaml
services:
  webserver:
    image: ghcr.io/paperless-ngx/paperless-ngx:latest
    # ...keep the rest of your existing config (env, volumes, depends_on, ...)
    environment:
      PAPERLESS_ERECHNUNG_FOP_URL: http://fop:8080
    volumes:
      - ./custom-init:/custom-cont-init.d:ro

  fop:
    image: ghcr.io/bitbetterde/paperless-ngx-erechnung-fop:latest
    restart: unless-stopped
```

   Don't publish a port for the `fop` service; Paperless reaches it over the Compose network.

4. Restart your Compose stack:

```bash
docker compose up -d
```

The startup script runs every time the container starts, so it needs access to PyPI and installs the newest release each time. To stay on a known version, pin it in the script (e.g. `pip install paperless-ngx-erechnung==X.Y.Z`). If the install fails, Paperless still starts, just without the plugin — check the `[custom-init]` lines in the logs.

The FOP service listens on port 8080 and accepts `POST /render` (XSL-FO in, PDF out) and `GET /health`. It only reads its bundled fonts and refuses all other files and URLs. Optional environment variables: `PORT`, `THREADS` (default: number of CPUs), `MAX_REQUEST_BYTES` (default: 64 MB), `REQUEST_TIMEOUT_SECONDS` (time to receive a request, default: 60) and `RESPONSE_TIMEOUT_SECONDS` (time to render and send the PDF, default: 150; keep it above the plugin's 120-second render timeout).

### Docker with a custom image (FOP inside Paperless)

If you'd rather not run a second container, build a Paperless image that includes FOP and a Java runtime. Leave `PAPERLESS_ERECHNUNG_FOP_URL` unset; the plugin then uses the local `fop` binary.

1. Create the following `Dockerfile` next to your preferred `docker-compose.(XYZ).yml`:

```dockerfile
FROM ghcr.io/paperless-ngx/paperless-ngx:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
  default-jre-headless fop \
&& rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir paperless-ngx-erechnung
USER paperless
```

2. Let Compose build it by setting `build: .` on the `webserver` service instead of `image:`:

```yaml
services:
  webserver:
    build: .
    # ...keep the rest of your existing config (env, volumes, depends_on, ...)
```

3. Build and restart:

```bash
docker compose build webserver && docker compose up -d
```

### Bare-metal

If Paperless-ngx runs directly on the host, install Apache FOP and a JRE via the system package manager, then install the plugin into the same Python environment Paperless uses (commonly a venv under `/opt/paperless/`). Paths and service names depend on how you provisioned Paperless — substitute yours:

```bash
# System-side: FOP + JRE
sudo apt install default-jre-headless fop          # Debian/Ubuntu

# Plugin into Paperless's Python environment
sudo -u paperless /opt/paperless/.venv/bin/pip install paperless-ngx-erechnung

# Restart so the entry-point group is rescanned
sudo systemctl restart paperless-webserver paperless-consumer paperless-scheduler
```

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

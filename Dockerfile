FROM ghcr.io/paperless-ngx/paperless-ngx:beta

USER root
# Apache FOP renders KoSIT's xr-pdf.xsl output (XSL-FO) into the archive PDF.
# It's a JVM application, so we install a headless JRE alongside it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless fop \
    && rm -rf /var/lib/apt/lists/*
COPY ./paperless-ngx-erechnung /opt/paperless-ngx-erechnung
RUN pip install --no-cache-dir /opt/paperless-ngx-erechnung
USER paperless
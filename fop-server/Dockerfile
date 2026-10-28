# syntax=docker/dockerfile:1
# SPDX-License-Identifier: GPL-3.0-only
#
# Apache FOP as an HTTP service for paperless-ngx-erechnung.
# Build from the repository root so the vendored fonts can be copied in:
#
#   docker build -f fop-server/Dockerfile -t paperless-ngx-erechnung-fop .

FROM maven:3.9-eclipse-temurin-25 AS build
WORKDIR /build
COPY fop-server/pom.xml .
RUN mvn -B -q dependency:go-offline
COPY fop-server/src ./src
RUN mvn -B -q package

FROM eclipse-temurin:25-jre
RUN useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin fop

COPY --from=build /build/target/fop-server.jar /opt/fop/fop-server.jar
COPY --from=build /build/target/lib /opt/fop/lib
COPY src/paperless_ngx_erechnung/xslt/conf/fonts /opt/fop/fonts
COPY src/paperless_ngx_erechnung/xslt/conf/fop.xconf /opt/fop/fop.xconf
# Same config the plugin uses locally, with absolute font paths and the
# font cache off (the service runs as a user without a home directory).
RUN sed -i \
      -e 's|embed-url="fonts/|embed-url="file:///opt/fop/fonts/|' \
      -e 's|<fop version="1.0">|<fop version="1.0">\n  <use-cache>false</use-cache>|' \
      /opt/fop/fop.xconf

USER fop
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-Djava.awt.headless=true", \
            "-cp", "/opt/fop/fop-server.jar:/opt/fop/lib/*", \
            "de.bitbetter.erechnung.fop.FopServer"]

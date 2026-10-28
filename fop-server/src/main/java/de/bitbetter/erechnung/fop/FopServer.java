// SPDX-License-Identifier: GPL-3.0-only
package de.bitbetter.erechnung.fop;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.xml.XMLConstants;
import javax.xml.parsers.SAXParserFactory;
import org.apache.fop.apps.FOUserAgent;
import org.apache.fop.apps.Fop;
import org.apache.fop.apps.FopConfParser;
import org.apache.fop.apps.FopFactory;
import org.apache.fop.apps.MimeConstants;
import org.apache.xmlgraphics.io.Resource;
import org.apache.xmlgraphics.io.ResourceResolver;
import org.apache.fop.apps.io.ResourceResolverFactory;
import org.xml.sax.InputSource;
import org.xml.sax.XMLReader;

/**
 * HTTP service that renders XSL-FO to PDF with an embedded Apache FOP.
 *
 * <ul>
 *   <li>{@code POST /render} — request body is XSL-FO, response is the PDF.
 *       Render errors return 422 with the FOP message as plain text.</li>
 *   <li>{@code GET /health} — returns 200 "ok".</li>
 * </ul>
 *
 * <p>The input comes from untrusted invoices, so the service refuses
 * DOCTYPE declarations and only lets FOP load files from the fonts
 * directory. Inline {@code data:} URIs (used for invoice attachments) are
 * handled inside FOP and never reach the resolver.
 */
public final class FopServer {

    private static final Logger LOG = Logger.getLogger(FopServer.class.getName());

    private final FopFactory fopFactory;
    private final long maxRequestBytes;

    private FopServer(FopFactory fopFactory, long maxRequestBytes) {
        this.fopFactory = fopFactory;
        this.maxRequestBytes = maxRequestBytes;
    }

    public static void main(String[] args) throws Exception {
        File config = new File(env("FOP_CONFIG", "/opt/fop/fop.xconf"));
        Path fontsDir = Path.of(env("FOP_FONTS_DIR", "/opt/fop/fonts")).toRealPath();
        int port = Integer.parseInt(env("PORT", "8080"));
        long maxRequestBytes = Long.parseLong(env("MAX_REQUEST_BYTES", String.valueOf(64L * 1024 * 1024)));
        int threads = Integer.parseInt(env("THREADS", String.valueOf(Runtime.getRuntime().availableProcessors())));
        // The JDK server never times out slow clients unless told to. Both
        // values are in seconds. The response timer starts once the request
        // body is read, so it includes rendering and must stay above the
        // plugin's 120 s render timeout. Must be set before the server starts.
        setDefaultProperty("sun.net.httpserver.maxReqTime", env("REQUEST_TIMEOUT_SECONDS", "60"));
        setDefaultProperty("sun.net.httpserver.maxRspTime", env("RESPONSE_TIMEOUT_SECONDS", "150"));

        FopFactory factory = new FopConfParser(config, new FontsOnlyResolver(fontsDir))
                .getFopFactoryBuilder()
                .build();
        FopServer service = new FopServer(factory, maxRequestBytes);

        ExecutorService pool = Executors.newFixedThreadPool(threads);
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/render", service::handleRender);
        server.createContext("/health", FopServer::handleHealth);
        server.setExecutor(pool);
        server.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.stop(5);
            pool.shutdown();
        }));
        LOG.info(() -> "FOP server listening on port " + port + " with " + threads + " worker threads");
    }

    private void handleRender(HttpExchange exchange) throws IOException {
        try {
            if (!"POST".equals(exchange.getRequestMethod())) {
                exchange.getResponseHeaders().set("Allow", "POST");
                sendText(exchange, 405, "Use POST with an XSL-FO request body.");
                return;
            }
            byte[] fo = readLimited(exchange.getRequestBody(), maxRequestBytes);
            if (fo == null) {
                sendText(exchange, 413, "Request body exceeds " + maxRequestBytes + " bytes.");
                return;
            }

            long started = System.nanoTime();
            ByteArrayOutputStream pdf = new ByteArrayOutputStream();
            try {
                render(fo, pdf);
            } catch (Exception e) {
                LOG.log(Level.WARNING, "Rendering failed", e);
                sendText(exchange, 422, describe(e));
                return;
            }
            long millis = (System.nanoTime() - started) / 1_000_000;
            LOG.info(() -> "Rendered " + fo.length + " bytes of FO to " + pdf.size() + " bytes of PDF in " + millis + " ms");
            send(exchange, 200, "application/pdf", pdf.toByteArray());
        } finally {
            exchange.close();
        }
    }

    private static void handleHealth(HttpExchange exchange) throws IOException {
        try {
            sendText(exchange, 200, "ok");
        } finally {
            exchange.close();
        }
    }

    private void render(byte[] fo, OutputStream out) throws Exception {
        FOUserAgent userAgent = fopFactory.newFOUserAgent();
        Fop fop = fopFactory.newFop(MimeConstants.MIME_PDF, userAgent, out);

        SAXParserFactory parserFactory = SAXParserFactory.newInstance();
        parserFactory.setNamespaceAware(true);
        parserFactory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
        parserFactory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        XMLReader reader = parserFactory.newSAXParser().getXMLReader();
        reader.setContentHandler(fop.getDefaultHandler());
        reader.parse(new InputSource(new ByteArrayInputStream(fo)));
    }

    /** Reads at most {@code limit} bytes; returns null if the body is larger. */
    private static byte[] readLimited(InputStream in, long limit) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        byte[] chunk = new byte[64 * 1024];
        long total = 0;
        int n;
        while ((n = in.read(chunk)) != -1) {
            total += n;
            if (total > limit) {
                return null;
            }
            buffer.write(chunk, 0, n);
        }
        return buffer.toByteArray();
    }

    private static String describe(Throwable e) {
        StringBuilder message = new StringBuilder();
        for (Throwable t = e; t != null; t = t.getCause()) {
            String text = t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
            if (message.indexOf(text) < 0) {
                if (message.length() > 0) {
                    message.append(": ");
                }
                message.append(text);
            }
            if (t.getCause() == t) {
                break;
            }
        }
        return message.toString();
    }

    private static void sendText(HttpExchange exchange, int status, String text) throws IOException {
        send(exchange, status, "text/plain; charset=utf-8", text.getBytes(StandardCharsets.UTF_8));
    }

    private static void send(HttpExchange exchange, int status, String contentType, byte[] body) throws IOException {
        exchange.getResponseHeaders().set("Content-Type", contentType);
        exchange.sendResponseHeaders(status, body.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(body);
        }
    }

    /** Sets a system property unless it was already given with -D. */
    private static void setDefaultProperty(String name, String value) {
        if (System.getProperty(name) == null) {
            System.setProperty(name, value);
        }
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }

    /** Lets FOP read files from the fonts directory and nothing else. */
    private static final class FontsOnlyResolver implements ResourceResolver {

        private final ResourceResolver delegate = ResourceResolverFactory.createDefaultResourceResolver();
        private final Path fontsDir;

        FontsOnlyResolver(Path fontsDir) {
            this.fontsDir = fontsDir;
        }

        @Override
        public Resource getResource(URI uri) throws IOException {
            if (!"file".equalsIgnoreCase(uri.getScheme())) {
                throw new IOException("Access to " + uri + " is not allowed");
            }
            Path path;
            try {
                path = Path.of(uri).toRealPath();
            } catch (IOException | IllegalArgumentException e) {
                throw new IOException("Access to " + uri + " is not allowed", e);
            }
            if (!path.startsWith(fontsDir)) {
                throw new IOException("Access to " + uri + " is not allowed");
            }
            return delegate.getResource(uri);
        }

        @Override
        public OutputStream getOutputStream(URI uri) throws IOException {
            throw new IOException("Writing to " + uri + " is not allowed");
        }
    }
}

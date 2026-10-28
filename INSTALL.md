# Installing paperless-ngx-erechnung

Two pieces need to be in place:

1. **The plugin**, installed into the Python environment Paperless runs under, so Paperless finds its parsers at startup.
2. **Apache FOP**, which turns XRechnung invoices into archive PDFs. FOP is a Java application. It can run as a separate container next to Paperless (recommended for Docker), or as a local `fop` binary with a Java runtime.

Once both are in place, Paperless picks up the parsers at startup. Look for `Loaded third-party parser 'XRechnung' …` in the logs to confirm the installation worked.

Ways to install:

- [Docker Compose with the FOP service](#docker-compose-with-the-fop-service-recommended) (recommended): the official Paperless-ngx image stays unchanged.
- [Docker with a custom image](#docker-with-a-custom-image): FOP and Java inside the Paperless image, no second container.
- [Bare metal](#bare-metal): Paperless runs directly on the host.

## Docker Compose with the FOP service (recommended)

FOP runs in its own container (`ghcr.io/bitbetterde/paperless-ngx-erechnung-fop`), and the plugin is added to the official Paperless-ngx image without building a custom image.

Start from a working Paperless-ngx Compose setup (e.g. [one of the official compose files](https://github.com/paperless-ngx/paperless-ngx/tree/main/docker/compose)).

### Step 1: Add the FOP service

Add the `fop` service and point Paperless at it in your `docker-compose.yml`:

```yaml
services:
  webserver:
    image: ghcr.io/paperless-ngx/paperless-ngx:latest
    # ...keep the rest of your existing config (env, volumes, depends_on, ...)
    environment:
      PAPERLESS_ERECHNUNG_FOP_URL: http://fop:8080

  fop:
    image: ghcr.io/bitbetterde/paperless-ngx-erechnung-fop:latest
    restart: unless-stopped
```

Don't publish a port for the `fop` service; Paperless reaches it over the Compose network.

### Step 2: Install the plugin

Pick one of the two options.

#### Option A: Startup script (recommended)

Paperless runs scripts from `/custom-cont-init.d` at every container start. A script there installs the plugin from PyPI.

1. Next to your `docker-compose.yml`, create a folder `custom-init` with a script `install-erechnung.sh`:

   ```bash
   #!/bin/bash
   pip install --no-cache-dir paperless-ngx-erechnung
   ```

2. Paperless only runs startup scripts owned by `root` and not writable by others:

   ```bash
   sudo chown -R root:root custom-init
   sudo chmod 755 custom-init custom-init/install-erechnung.sh
   ```

3. Mount the folder:

   ```yaml
   services:
     webserver:
       volumes:
         - ./custom-init:/custom-cont-init.d:ro
   ```

4. Restart the stack with `docker compose up -d`.

Because the script runs at every start, it needs access to PyPI and installs the newest release each time. To stay on a known version, pin it in the script (e.g. `pip install paperless-ngx-erechnung==X.Y.Z`). If the install fails, Paperless still starts, just without the plugin; check the `[custom-init]` lines in the logs.

#### Option B: Mounted plugin folder

Install the plugin into a folder once and mount it into the container. Paperless then needs no PyPI access at startup, and the plugin version only changes when you reinstall the folder.

1. Install the plugin into `./plugins`. Run this with the Paperless image, so the compiled parts match its Python version and CPU:

   ```bash
   docker run --rm --entrypoint pip -v ./plugins:/plugins \
     ghcr.io/paperless-ngx/paperless-ngx:latest \
     install --target /plugins --no-deps paperless-ngx-erechnung saxonche
   ```

2. Mount the folder and add it to Python's search path:

   ```yaml
   services:
     webserver:
       environment:
         PYTHONPATH: /usr/src/paperless/plugins
       volumes:
         - ./plugins:/usr/src/paperless/plugins:ro
   ```

3. Restart the stack with `docker compose up -d`.

To upgrade the plugin, empty the folder and run step 1 again.

Things to know about this option:

- **Rerun step 1 after Paperless upgrades that change Python.** `saxonche` is compiled for one Python version and CPU type. If Paperless moves to a new Python version, or you move the setup to another CPU type, the plugin no longer loads until you reinstall the folder.
- **Keep `--no-deps`.** The Paperless image already ships the plugin's other dependencies (`lxml`, `pikepdf`, `pypdfium2`, `Pillow`). Folders on `PYTHONPATH` take priority over the image's own packages, so installing those into the folder would replace the versions Paperless was built with.

### FOP service settings

The FOP service listens on port 8080 and accepts `POST /render` (XSL-FO in, PDF out) and `GET /health`. It only reads its bundled fonts and refuses all other files and URLs.

Optional environment variables for the `fop` service:

| Variable                   | Default          | Meaning                                                                                   |
|----------------------------|------------------|-------------------------------------------------------------------------------------------|
| `PORT`                     | `8080`           | Port to listen on                                                                         |
| `THREADS`                  | number of CPUs   | Renders that can run at the same time                                                     |
| `MAX_REQUEST_BYTES`        | 64 MB            | Largest accepted request                                                                  |
| `REQUEST_TIMEOUT_SECONDS`  | `60`             | Time a client gets to send its request                                                    |
| `RESPONSE_TIMEOUT_SECONDS` | `150`            | Time to render and send the PDF; keep it above the plugin's 120-second render timeout    |

## Docker with a custom image

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

## Bare metal

If Paperless-ngx runs directly on the host, install Apache FOP and a JRE via the system package manager, then install the plugin into the same Python environment Paperless uses (commonly a venv under `/opt/paperless/`). Paths and service names depend on how you provisioned Paperless; substitute yours:

```bash
# System-side: FOP + JRE
sudo apt install default-jre-headless fop          # Debian/Ubuntu

# Plugin into Paperless's Python environment
sudo -u paperless /opt/paperless/.venv/bin/pip install paperless-ngx-erechnung

# Restart so the entry-point group is rescanned
sudo systemctl restart paperless-webserver paperless-consumer paperless-scheduler
```

You can also use the FOP service container with a bare-metal Paperless: run it with a port published on `127.0.0.1` and set `PAPERLESS_ERECHNUNG_FOP_URL` (e.g. `http://localhost:8080`) in Paperless's environment instead of installing FOP and Java.

# SPDX-License-Identifier: GPL-3.0-only
"""Tests for rendering through the FOP service (``PAPERLESS_ERECHNUNG_FOP_URL``).

A stub HTTP server stands in for the ``fop-server`` container, so these run
without Java.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from paperless_ngx_erechnung import rendering
from paperless_ngx_erechnung.rendering import FOP_URL_ENV, FopNotAvailableError

FAKE_PDF = b"%PDF-1.4\n% stub\n%%EOF\n"


class _StubFop:
    """Records requests and replies with a configurable status and body."""

    def __init__(self) -> None:
        self.status = 200
        self.body = FAKE_PDF
        self.requests: list[tuple[str, str, str | None, bytes]] = []


@pytest.fixture
def stub_fop() -> Iterator[tuple[str, _StubFop]]:
    state = _StubFop()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            state.requests.append(
                ("POST", self.path, self.headers.get("Content-Type"), body)
            )
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()


def test_posts_fo_to_render_endpoint(
    stub_fop: tuple[str, _StubFop], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, state = stub_fop
    monkeypatch.setenv(FOP_URL_ENV, url + "/")  # trailing slash is tolerated
    out = tmp_path / "nested" / "out.pdf"

    rendering._fo_to_pdf(b"<fo:root/>", out)

    assert out.read_bytes() == FAKE_PDF
    assert state.requests == [("POST", "/render", "application/xml", b"<fo:root/>")]


def test_service_used_even_without_local_fop(
    stub_fop: tuple[str, _StubFop], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = stub_fop
    monkeypatch.setenv(FOP_URL_ENV, url)
    monkeypatch.setattr(rendering.shutil, "which", lambda _name: None)

    rendering._fo_to_pdf(b"<fo:root/>", tmp_path / "out.pdf")

    assert (tmp_path / "out.pdf").is_file()


def test_service_error_includes_status_and_message(
    stub_fop: tuple[str, _StubFop], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, state = stub_fop
    state.status = 422
    state.body = b"fo:root is missing child elements"
    monkeypatch.setenv(FOP_URL_ENV, url)

    with pytest.raises(RuntimeError, match=r"HTTP 422.*missing child elements"):
        rendering._fo_to_pdf(b"<fo:root/>", tmp_path / "out.pdf")
    assert not (tmp_path / "out.pdf").exists()


def test_non_pdf_response_is_rejected(
    stub_fop: tuple[str, _StubFop], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, state = stub_fop
    state.body = b"<html>not a pdf</html>"
    monkeypatch.setenv(FOP_URL_ENV, url)

    with pytest.raises(RuntimeError, match="not a PDF"):
        rendering._fo_to_pdf(b"<fo:root/>", tmp_path / "out.pdf")


def test_unreachable_service_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv(FOP_URL_ENV, f"http://127.0.0.1:{port}")

    with pytest.raises(RuntimeError, match="Could not reach FOP service"):
        rendering._fo_to_pdf(b"<fo:root/>", tmp_path / "out.pdf")


def test_non_http_url_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FOP_URL_ENV, "file:///etc")

    with pytest.raises(RuntimeError, match="must be an http"):
        rendering._fo_to_pdf(b"<fo:root/>", tmp_path / "out.pdf")


def test_missing_service_and_binary_explains_both_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FOP_URL_ENV, raising=False)
    monkeypatch.setattr(rendering.shutil, "which", lambda _name: None)

    with pytest.raises(FopNotAvailableError, match=FOP_URL_ENV):
        rendering._fo_to_pdf(b"<fo:root/>", tmp_path / "out.pdf")

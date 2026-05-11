# SPDX-License-Identifier: GPL-3.0-only
"""Shared pytest fixtures and sample-file paths.

Note on the ``documents.parsers`` shim:

The plugin's ``parsers.py`` raises ``documents.parsers.ParseError`` to match
the contract Paperless-ngx's consumer expects. That module obviously isn't
importable in this repo's own test run. We inject a tiny stand-in into
``sys.modules`` before any test imports the plugin, so ``parse()`` can be
exercised end-to-end without pulling in all of paperless-ngx.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


def _install_documents_shim() -> None:
    if "documents.parsers" in sys.modules:
        return

    documents = types.ModuleType("documents")
    parsers_mod = types.ModuleType("documents.parsers")

    class ParseError(Exception):
        """Stand-in for paperless-ngx's ``documents.parsers.ParseError``."""

    parsers_mod.ParseError = ParseError  # type: ignore[attr-defined]
    documents.parsers = parsers_mod  # type: ignore[attr-defined]

    sys.modules["documents"] = documents
    sys.modules["documents.parsers"] = parsers_mod


_install_documents_shim()


SAMPLES_DIR = Path(__file__).parent / "samples"


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    """Directory containing test invoice samples."""
    return SAMPLES_DIR


@pytest.fixture(scope="session")
def ubl_invoice_path(samples_dir: Path) -> Path:
    """KoSIT ``maxRechnung_ubl.xml`` — a comprehensive UBL Invoice sample."""
    return samples_dir / "ubl_invoice_sample.xml"


@pytest.fixture(scope="session")
def ubl_creditnote_path(samples_dir: Path) -> Path:
    """KoSIT ``maxRechnung_creditnote.xml`` — UBL CreditNote sample."""
    return samples_dir / "ubl_creditnote_sample.xml"


@pytest.fixture(scope="session")
def cii_invoice_path(samples_dir: Path) -> Path:
    """KoSIT included-notes-bg-1-uncefact.xml — CII CrossIndustryInvoice."""
    return samples_dir / "cii_sample.xml"


@pytest.fixture(scope="session")
def ubl_invoice_bytes(ubl_invoice_path: Path) -> bytes:
    return ubl_invoice_path.read_bytes()


@pytest.fixture(scope="session")
def ubl_creditnote_bytes(ubl_creditnote_path: Path) -> bytes:
    return ubl_creditnote_path.read_bytes()


@pytest.fixture(scope="session")
def cii_invoice_bytes(cii_invoice_path: Path) -> bytes:
    return cii_invoice_path.read_bytes()

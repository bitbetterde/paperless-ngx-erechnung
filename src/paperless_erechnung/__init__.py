# SPDX-License-Identifier: GPL-3.0-only
"""German E-Rechnung parser plugin for Paperless-ngx.

Registers two parsers under the ``paperless_ngx.parsers`` entry-point group:

- :class:`paperless_erechnung.parsers.XRechnungParser` for pure-XML XRechnung
  (UBL Invoice, UBL CreditNote, UN/CEFACT CII).
- :class:`paperless_erechnung.parsers.ZUGFeRDParser` for ZUGFeRD/Factur-X
  hybrid PDF/A-3 invoices.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]

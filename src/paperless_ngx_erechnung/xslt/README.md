# Vendored KoSIT XRechnung Visualization

This directory contains a pinned snapshot of the KoSIT
[XRechnung-Visualization](https://github.com/itplr-kosit/xrechnung-visualization)
stylesheets (Apache-2.0).

## Pinned version

**Vendored from upstream tag `v2026-01-31`** (compatible with XRechnung 3.0.x).
See `VENDORED_VERSION.txt` for the exact commit.

To re-vendor / bump:

```bash
cd /tmp
rm -rf xrechnung-visualization
git clone https://github.com/itplr-kosit/xrechnung-visualization.git
cd xrechnung-visualization
git checkout <TAG>     # e.g. v2026-01-31
cp -R src/xsl/. <this-dir>/
cp LICENSE <this-dir>/LICENSE
echo "Pinned to: <TAG>" > <this-dir>/VENDORED_VERSION.txt
```

Files this plugin uses (paths relative to this directory):

| File                       | Purpose                                |
|----------------------------|----------------------------------------|
| `ubl-invoice-xr.xsl`       | UBL Invoice → XR (intermediate)        |
| `ubl-creditnote-xr.xsl`    | UBL CreditNote → XR                    |
| `cii-xr.xsl`               | UN/CEFACT CII → XR                     |
| `xrechnung-html.xsl`       | XR → HTML (rendered to PDF downstream) |

`paperless_ngx_erechnung.rendering` references these by filename; update both
sides if the upstream renames anything.

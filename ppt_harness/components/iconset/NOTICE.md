# Vendored icons

`paths.json` holds a curated subset of **Tabler Icons** (outline style), redrawn as plain
`M`/`L`/`C`/`Z` path data.

- Source: <https://github.com/tabler/tabler-icons>, `icons/outline/`
- Commit: `7007ad52` (recorded in `paths.json` under `commit`)
- Copyright (c) 2020-2026 Paweł Kuna
- Licence: MIT — the full text is in `LICENSE`, beside this file

145 of the library's 5,130 icons are here. The whole set is not vendored: a deck needs
about a hundred and thirty concepts, and the rest would be a megabyte of path data no
component can name. `scripts/vendor_icons.py` holds the curation list and rebuilds this
file from a Tabler checkout, so which icons are here and where they came from is code
rather than folklore.

The only change to the artwork is a **normalisation**: Tabler draws round corners and
circles with SVG elliptic arcs (`A`), which OOXML parametrises differently — centre plus
sweep angle, against SVG's endpoint form. Every arc is converted to cubic Béziers at
vendor time so the exporter and the HTML preview consume the identical string. No
coordinate is moved and no icon is redrawn; the paths are geometrically the originals.

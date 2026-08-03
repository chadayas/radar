#!/usr/bin/env python3
"""Convert the fixed BOM into Mouser's copy-&-paste tool format.

Emits one "<part>|<qty>" per line. Uses the re-sourced Mouser part number
(mouser_pn column) since that's the part actually being ordered; falls back to
the manufacturer part number (mpn) only if no Mouser PN is set. Quantity is the
board qty; blank -> 1 (Mouser's default).

Input may be a plain CSV or a Gnumeric workbook saved with a .csv name (gzipped
XML) -- the latter is auto-converted with ssconvert.

Usage: python3 mouser_bom2paste.py [mouser_bom_fixed.csv] [bom/mouser_paste.txt]
"""
import csv
import subprocess
import sys
import tempfile

SRC = "mouser_bom_fixed.csv"
OUT = "bom/mouser_paste.txt"


def clean(s):
    """Strip and collapse whitespace (kills the stray newlines in cells)."""
    return " ".join((s or "").split()).strip()


def load_rows(path):
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":  # gzip -> Gnumeric workbook; convert to CSV first
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        subprocess.run(["ssconvert", path, tmp], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = tmp
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else SRC
    out = sys.argv[2] if len(sys.argv) > 2 else OUT

    rows = load_rows(src)
    lines, skipped = [], []
    for r in rows:
        pn = clean(r.get("mouser_pn")) or clean(r.get("mpn"))
        qty = clean(r.get("qty")) or "1"
        if not pn:
            skipped.append(clean(r.get("designators")) or "?")
            continue
        lines.append(f"{pn}|{qty}")

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"wrote {out}: {len(lines)} parts")
    if skipped:
        print(f"  skipped {len(skipped)} row(s) with no part number: "
              + ", ".join(skipped))


if __name__ == "__main__":
    sys.exit(main())

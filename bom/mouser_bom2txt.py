#!/usr/bin/env python3
"""Reconcile the Mouser BOM-tool page against the uploaded BOM.

Reads the saved "source code" of the Mouser BOM edit page (mouser_cart.html)
and the BOM that was uploaded (bom_passives.csv), then reports which lines
Mouser matched & stocked vs. which came back NO MATCH / NOT ORDERABLE /
BACKORDER -- i.e. the parts that need a new manufacturer.

The Mouser page has no value/package/designator info for the lines it could
not match, so each Mouser line is joined back to bom_passives.csv (same upload
order, cross-checked by MPN + qty) to recover what the part actually is.

Inputs  : mouser_cart.html   Mouser BOM edit page, saved via "view source"
          bom_passives.csv   the uploaded BOM (Value,Package,Qty,Designators,
                             Mfr,Mfr Part Number,...)
Outputs : mouser_bom_status.txt   human-readable, problem parts first
          mouser_bom_status.csv   one row per line, STATUS column, sortable

Usage   : python3 mouser_bom2txt.py [mouser_cart.html] [bom_passives.csv]
"""
import csv
import re
import sys

from bs4 import BeautifulSoup

HTML = "mouser_cart.html"
BOM = "bom/bom_passives.csv"
OUT_TXT = "mouser_bom_status.txt"
OUT_CSV = "bom/mouser_bom_status.csv"          # every line, STATUS column
OUT_CSV_IN = "bom/mouser_bom_instock.csv"      # IN_STOCK only
OUT_CSV_OUT = "bom/mouser_bom_not_instock.csv"  # no-match + not-orderable + backorder

# Designators of parts already on hand -- dropped from the actionable reports
# (still counted for the Mouser reconciliation so the parse check stays honest).
ON_HAND = {"L1"}  # 2.2uH inductor: already have it

# Line statuses, worst first (drives report + sort order).
NO_MATCH = "NO_MATCH"            # Mouser found nothing for the MPN
NOT_ORDERABLE = "NOT_ORDERABLE"  # matched but obsolete / not stocked (N/A)
BACKORDER = "BACKORDER"          # orderable but 0 (or short) in stock / long lead
IN_STOCK = "IN_STOCK"           # matched and enough ships now
STATUS_ORDER = [NO_MATCH, NOT_ORDERABLE, BACKORDER, IN_STOCK]
STATUS_ICON = {NO_MATCH: "X", NOT_ORDERABLE: "!", BACKORDER: "~", IN_STOCK: "OK"}
STATUS_TITLE = {
    NO_MATCH: "NO MATCH -- Mouser found nothing; pick a new manufacturer",
    NOT_ORDERABLE: "NOT ORDERABLE -- matched but obsolete / not stocked; re-source",
    BACKORDER: "BACKORDER / LONG LEAD -- orderable but delayed (0 or short stock)",
    IN_STOCK: "IN STOCK -- matched and available",
}


def clean(s):
    """Collapse whitespace (incl. &nbsp;) and strip."""
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm(s):
    """Loose part-number key: lowercase alnum only (kills dashes/spaces)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def num(s):
    """First integer in a string (commas stripped), or None."""
    m = re.search(r"([\d,]+)", s or "")
    return int(m.group(1).replace(",", "")) if m else None


def is_on_hand(row):
    """True if every designator on this line is a part we already have."""
    refs = {d.strip() for d in (row.get("designators") or "").split(",") if d.strip()}
    return bool(refs) and refs <= ON_HAND


CSV_COLS = ["status", "value", "package", "qty", "designators", "mpn",
            "mfr", "mouser_pn", "availability", "risk", "notes"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CSV_COLS})


# ---------------------------------------------------------------- BOM (uploaded)

def load_bom(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- Mouser page

def labeled_values(td):
    """Map bold label -> value for a detail cell.

    Two layouts appear: matched rows put label+value as sibling divs in one
    .row; unmatched rows put the value in the following .row. Handle both.
    """
    out = {}
    for lab in td.select("div.font-weight-bold"):
        # Drop all periods so "Mfr. No" -> "mfr no", "Desc." -> "desc".
        key = re.sub(r"\s+", " ", clean(lab.get_text()).replace(".", "").lower()).strip()
        if not key:
            continue
        val = ""
        sib = lab.find_next_sibling("div")
        if sib and "font-weight-bold" not in (sib.get("class") or []):
            val = clean(sib.get_text())
        if not val:
            row = lab.find_parent("div", class_="row")
            nxt = row.find_next_sibling("div", class_="row") if row else None
            if nxt:
                val = clean(nxt.get_text())
        out.setdefault(key, val)
    return out


def parse_html(path):
    with open(path, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    # Mouser's own summary counts, for cross-checking our classification.
    summary = {}
    for opt in soup.select("#ddlFilter option"):
        m = re.match(r"(.+?)\s*\((\d+)\)\s*$", clean(opt.get_text()))
        if m:
            summary[m.group(1)] = int(m.group(2))

    body = soup.find("tbody", id="bomEditGridBody")
    if body is None:
        sys.exit(f"{path}: could not find <tbody id='bomEditGridBody'> -- "
                 "is this the saved Mouser BOM edit page?")

    items = []
    for tr in body.find_all("tr", attrs={"data-itemid": True}, recursive=False):
        tds = tr.find_all("td", recursive=False)
        detail = tds[1] if len(tds) > 1 else tr
        row_text = clean(tr.get_text(" ", strip=True))
        low = row_text.lower()
        labels = labeled_values(detail)

        rec = {"itemid": tr.get("data-itemid")}

        qi = tr.find("input", attrs={"data-testid": "BomEditQuantityInput"})
        rec["req_qty"] = num(qi.get("data-origqty")) if qi else None

        link = tr.find("a", attrs={"data-testid": "lnkMouserPartNumber"})
        rec["mouser_pn"] = clean(link.get_text()) if link else ""

        # Scope to the "No Match Found For ..." node itself -- reading the whole
        # row would let the trailing N/A columns bleed into the captured MPN.
        nm_node = tr.find(string=re.compile(r"No Match Found For"))
        rec["no_match"] = nm_node is not None
        nm_pn = ""
        if nm_node:
            m = re.search(r"No Match Found For\s*(\S+)?", clean(nm_node))
            nm_pn = clean(m.group(1)) if (m and m.group(1)) else ""

        rec["mfr_pn"] = labels.get("mfr no") or nm_pn
        rec["mfr"] = labels.get("mfr", "")
        rec["desc"] = labels.get("desc", "")

        icon = tr.find("i", class_=re.compile(r"\brisk-(low|medium|high)\b"))
        rec["risk"] = clean(icon.get("aria-label") or icon.get("data-content")) if icon else ""

        sn = re.search(r"([\d,]+)\s+Ships Now", row_text)
        rec["ships_now"] = int(sn.group(1).replace(",", "")) if sn else None
        bo = re.search(r"([\d,]+)\s+Backordered", row_text)
        rec["backordered"] = int(bo.group(1).replace(",", "")) if bo else 0
        rec["long_lead"] = "long lead time" in low

        rec["status"] = classify(rec)
        items.append(rec)

    return items, summary, soup


def classify(rec):
    if rec["no_match"]:
        return NO_MATCH
    req = rec["req_qty"] or 0
    ships = rec["ships_now"]
    if ships is not None and req and ships >= req:
        return IN_STOCK
    if rec["backordered"] or rec["long_lead"]:
        return BACKORDER
    if ships:  # >0 but short of req
        return BACKORDER
    # matched, nothing ships, no backorder path -> obsolete / limited / N/A
    return NOT_ORDERABLE


# ---------------------------------------------------------------- join + report

def avail_str(rec):
    if rec["status"] == NO_MATCH:
        return "-"
    bits = []
    if rec["ships_now"] is not None:
        bits.append(f"{rec['ships_now']} ships now")
    if rec["backordered"]:
        bits.append(f"{rec['backordered']} backordered")
    if rec["long_lead"]:
        bits.append("long lead")
    if not bits:
        bits.append("N/A")
    return ", ".join(bits)


def join(items, bom):
    """Pair each Mouser line with its uploaded BOM row.

    Upload order is preserved, so pair positionally, but verify by MPN and warn
    on any drift so a silently mis-aligned report can't slip through.
    """
    warnings = []
    if len(items) != len(bom):
        warnings.append(f"line count differs: Mouser={len(items)} BOM={len(bom)}")
    rows = []
    for i, it in enumerate(items):
        b = bom[i] if i < len(bom) else {}
        bmpn = b.get("Mfr Part Number", "")
        if it["mfr_pn"] and bmpn and norm(it["mfr_pn"]) != norm(bmpn):
            warnings.append(
                f"row {i+1}: MPN mismatch Mouser='{it['mfr_pn']}' BOM='{bmpn}'")
        rows.append({
            "status": it["status"],
            "value": b.get("Value", ""),
            "package": b.get("Package", ""),
            "qty": it["req_qty"] if it["req_qty"] is not None else num(b.get("Qty", "")),
            "designators": b.get("Designators", ""),
            "mpn": it["mfr_pn"] or bmpn,
            "mfr": it["mfr"] or b.get("Mfr", ""),
            "mouser_pn": it["mouser_pn"],
            "availability": avail_str(it),
            "risk": it["risk"],
            "notes": b.get("Notes", ""),
        })
    return rows, warnings


def fmt_table(rows, cols):
    widths = {c: len(h) for c, h in cols}
    for r in rows:
        for c, _ in cols:
            widths[c] = max(widths[c], len(str(r.get(c, ""))))
    line = "  " + "  ".join(h.ljust(widths[c]) for c, h in cols)
    out = [line, "  " + "  ".join("-" * widths[c] for c, _ in cols)]
    for r in rows:
        out.append("  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c, h in cols))
    return "\n".join(out)


def write_reports(rows, summary, warnings, excluded):
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_ORDER}
    pcs = {s: sum((r["qty"] or 0) for r in rows if r["status"] == s) for s in STATUS_ORDER}

    L = []
    L.append("Mouser BOM Match Status -- radar2 passives")
    L.append("Source: mouser_cart.html (Mouser BOM tool) x bom_passives.csv")
    if summary:
        L.append("Mouser summary: " + " | ".join(
            f"{k} {v}" for k, v in summary.items()
            if k in ("All Lines", "Exact Matches", "No Matches", "Not Orderable")))
    L.append("Computed:       " + " | ".join(
        f"{STATUS_ICON[s]} {counts[s]} {s.lower()} ({pcs[s]} pcs)" for s in STATUS_ORDER))
    need = counts[NO_MATCH] + counts[NOT_ORDERABLE] + counts[BACKORDER]
    L.append(f"Action:         {need} of {len(rows)} lines need attention "
             f"({counts[NO_MATCH] + counts[NOT_ORDERABLE]} need a new manufacturer)")
    if excluded:
        L.append("Excluded (already on hand): " +
                 ", ".join(f"{r['designators']} {r['value']}" for r in excluded))
    L.append("=" * 72)
    L.append("")

    prob_cols = [("value", "Value"), ("package", "Pkg"), ("qty", "Qty"),
                 ("designators", "Designators"), ("mpn", "Uploaded MPN"),
                 ("availability", "Availability"), ("notes", "Notes")]
    ok_cols = [("value", "Value"), ("package", "Pkg"), ("qty", "Qty"),
               ("mpn", "MPN"), ("mouser_pn", "Mouser No"), ("availability", "Availability")]

    for s in STATUS_ORDER:
        grp = [r for r in rows if r["status"] == s]
        if not grp:
            continue
        # Blank MPN == the BOM line never had a part chosen; make that explicit.
        disp = [{**r, "mpn": r["mpn"] or "(no MPN in BOM)"} for r in grp]
        L.append(f"[{STATUS_ICON[s]}] {STATUS_TITLE[s]}  ({len(grp)} lines, {pcs[s]} pcs)")
        L.append(fmt_table(disp, ok_cols if s == IN_STOCK else prob_cols))
        L.append("")

    if warnings:
        L.append("!! join warnings (check alignment):")
        L.extend(f"   - {w}" for w in warnings)
        L.append("")

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    ordered = sorted(rows, key=lambda r: order[r["status"]])
    write_csv(OUT_CSV, ordered)
    write_csv(OUT_CSV_IN, [r for r in ordered if r["status"] == IN_STOCK])
    write_csv(OUT_CSV_OUT, [r for r in ordered if r["status"] != IN_STOCK])

    return counts, pcs, need


def main():
    html = sys.argv[1] if len(sys.argv) > 1 else HTML
    bom_path = sys.argv[2] if len(sys.argv) > 2 else BOM

    items, summary, _ = parse_html(html)
    bom = load_bom(bom_path)
    rows, warnings = join(items, bom)

    # Reconcile against Mouser's summary on the FULL line set (before dropping
    # on-hand parts) so the parse cross-check stays valid.
    full = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_ORDER}

    report_rows = [r for r in rows if not is_on_hand(r)]
    excluded = [r for r in rows if is_on_hand(r)]
    counts, pcs, need = write_reports(report_rows, summary, warnings, excluded)

    print(f"parsed {len(items)} Mouser lines, {len(bom)} BOM lines")
    for s in STATUS_ORDER:
        print(f"  {STATUS_ICON[s]:>2}  {s:<14} {counts[s]:>2} lines  {pcs[s]:>4} pcs")
    if excluded:
        print(f"  excluded {len(excluded)} on-hand line(s): "
              + ", ".join(r["designators"] for r in excluded))
    print(f"wrote {OUT_TXT}, {OUT_CSV}, {OUT_CSV_IN}, {OUT_CSV_OUT}")

    # Cross-check against Mouser's own filter counts.
    if summary:
        checks = [
            ("no-match", full[NO_MATCH], summary.get("No Matches")),
            ("not-orderable(=no-match+obsolete)",
             full[NO_MATCH] + full[NOT_ORDERABLE], summary.get("Not Orderable")),
            ("exact(=in-stock+backorder+not-orderable)",
             full[IN_STOCK] + full[BACKORDER] + full[NOT_ORDERABLE],
             summary.get("Exact Matches")),
            ("total", len(rows), summary.get("All Lines")),
        ]
        print("reconcile vs Mouser summary:")
        for name, got, exp in checks:
            ok = "ok" if exp is not None and got == exp else "MISMATCH"
            print(f"  [{ok}] {name}: computed={got} mouser={exp}")
    for w in warnings:
        print(f"  warn: {w}")


if __name__ == "__main__":
    sys.exit(main())

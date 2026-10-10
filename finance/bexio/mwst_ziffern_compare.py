"""Compare bexio's payment-basis Ziffern per quarter with ERPNext's, Ziffer by Ziffer.

The two sides are the CSVs: bexio-mwst-ziffern.csv from mwst_bexio.py (the export alone) and
the mwst-<quarter>-payment.csv files of mwst_report.py --basis payment (ERPNext, read only).
Per quarter and form row the tax must be equal; the base (Entgelt) may differ within the
tolerance the bexio CSV carries, because ERPNext derives a document's base from its tax and
mwst_bexio states that bound per row. A base difference that is exactly the gap between the
two journal parts is the structural one: the report's journal rows carry no base for the
input tax (1170, 1171) and the Bezugsteuer (2203), while bexio's manual entries have their
nets. Everything else is a difference, and the run exits 1 while one stands.

The detail goes to one private CSV under <private>; the stdout shows counts only.

Run it as:

    python3 finance/bexio/mwst_ziffern_compare.py

Standard library only, plus import_master for the private path.
"""

import argparse
import csv
import glob
import os
import re
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import import_master as im

CENT = Decimal("0.01")
ZERO = Decimal("0")
JOURNAL_LABEL = "journal entries (by account)"
# a total row's journal part is its components': the report's CSV holds no journal sub-row for
# the totals, so the comparison sums the components' before it classifies a total
TOTALS = {"399": ("302", "303", "312", "313", "342", "343", "382", "383"),
          "420": ("400", "405", "415")}


def money(text):
    return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)


def read_bexio(path):
    """The bexio CSV: {quarter: {ziffer: (base, tax, journal_base, journal_tax, base_tol, tax_tol)}}."""
    out = defaultdict(dict)
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            out[row["quarter"]][row["ziffer"]] = (
                money(row["entgelt_chf"]), money(row["steuer_chf"]),
                money(row["journal_entgelt_chf"]), money(row["journal_steuer_chf"]),
                money(row["entgelt_tol_chf"]), money(row["steuer_tol_chf"]))
    return dict(out)


def read_erpnext(paths):
    """The report's per-quarter CSVs: {quarter: {ziffer: (base, tax, journal_base, journal_tax)}}.

    A row is the total of its Ziffer; a following row labelled as the journal's is its journal
    part, so the document part is the total less the journal."""
    out = defaultdict(dict)
    for path in paths:
        quarter = re.search(r"mwst-(\d{4}Q\d)-payment\.csv$", path)
        if quarter is None:
            continue
        rows = {}
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 4 or row[0] in ("ziffer", "") or not row[0].isdigit():
                    continue
                ziffer, label, base, tax = row[0], row[1], money(row[2]), money(row[3])
                if label == JOURNAL_LABEL:
                    prev = rows.get(ziffer, (ZERO, ZERO, ZERO, ZERO))
                    rows[ziffer] = (prev[0], prev[1], base, tax)
                elif ziffer in rows:
                    prev = rows[ziffer]
                    rows[ziffer] = (prev[0] + base, prev[1] + tax, prev[2], prev[3])
                else:
                    rows[ziffer] = (base, tax, ZERO, ZERO)
        out[quarter.group(1)] = rows
    return dict(out)


def reason_of(bexio, erp):
    """Why the two rows of one Ziffer differ, or '' when they do not.

    The comparison is of the form's figures: the document part (the row less its journal part)
    must hold within the tolerances the bexio CSV carries, and what then remains is the
    journal part: bexio's holds only the manual entries' VAT, the report's keeps the net of
    every VAT-account move its exclusions leave in, so a difference there is the mirrors'
    residue, named for the vouchers that cause it (see finance/docs/swiss.md)."""
    d_base = erp[0] - bexio[0]
    d_tax = erp[1] - bexio[1]
    if not d_base and not d_tax:
        return ""
    resid_base = d_base - (erp[2] - bexio[2])
    resid_tax = d_tax - (erp[3] - bexio[3])
    if abs(resid_base) > bexio[4] or abs(resid_tax) > bexio[5]:
        return "unexplained"
    if not (erp[2] - bexio[2]) and not (erp[3] - bexio[3]):
        return "rounding"
    return "journal mirrors"


def with_totals_journal(rows):
    """The quarter's rows with the totals' journal tax summed from their components', so a
    total is classified by the same rule as the rows it adds up. A total's base is zero on the
    form, so its journal base stays zero too."""
    out = dict(rows)
    for total, parts in TOTALS.items():
        row = out.get(total)
        if row is None:
            continue
        jtax = sum((out[p][3] for p in parts if p in out), ZERO)
        out[total] = (row[0], row[1], row[2], jtax)
    return out


def compare(bexio, erpnext):
    """One row per quarter and Ziffer on either side: the two bases and taxes, the tolerances
    of the quarter's row, and the reason the rows differ ('' when they do not). A quarter
    with no ERPNext CSV is a row of its own, so it shows as a difference."""
    rows = []
    for quarter in sorted(set(bexio) | set(erpnext)):
        b_rows = bexio.get(quarter, {})
        e_rows = erpnext.get(quarter)
        if e_rows is None:
            for ziffer in sorted(b_rows):
                b = b_rows[ziffer]
                rows.append((quarter, ziffer, b[0], ZERO, b[1], ZERO, b[4], b[5], "no ERPNext report"))
            continue
        e_rows = with_totals_journal(e_rows)
        for ziffer in sorted(set(b_rows) | set(e_rows)):
            b = b_rows.get(ziffer, (ZERO, ZERO, ZERO, ZERO, ZERO, ZERO))
            e = e_rows.get(ziffer, (ZERO, ZERO, ZERO, ZERO))
            rows.append((quarter, ziffer, b[0], e[0], b[1], e[1], b[4], b[5], reason_of(b, e)))
    return rows


def differences(rows):
    """The rows whose reason is not '' (the reason is the last column)."""
    return [row for row in rows if row[8]]


# the reasons a difference may carry and still be explained: the rounding the tolerances bound,
# and the journal mirrors' residue the report's journal rows keep (finance/docs/swiss.md)
EXPLAINED = ("rounding", "journal mirrors")


def unexplained(rows):
    """The rows whose difference no reason covers: the run fails while they stand."""
    return [row for row in rows if row[8] not in ("",) + EXPLAINED]


def write_csv(path, rows):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["quarter", "ziffer", "bexio_entgelt_chf", "erpnext_entgelt_chf",
                         "bexio_steuer_chf", "erpnext_steuer_chf", "entgelt_tol_chf",
                         "steuer_tol_chf", "reason"])
        for quarter, ziffer, b_base, e_base, b_tax, e_tax, b_tol, s_tol, why in rows:
            writer.writerow([quarter, ziffer, "{:.2f}".format(b_base), "{:.2f}".format(e_base),
                             "{:.2f}".format(b_tax), "{:.2f}".format(e_tax), "{:.2f}".format(b_tol),
                             "{:.2f}".format(s_tol), why])


def summary_lines(rows):
    """Counts only: the stdout names no amounts."""
    bad = differences(rows)
    by_reason = defaultdict(int)
    for row in bad:
        by_reason[row[8]] += 1
    left = unexplained(rows)
    lines = [
        "quarters compared: {}".format(len({row[0] for row in rows})),
        "Ziffern compared: {}".format(len(rows)),
        "Ziffern with a difference: {}".format(len(bad)),
    ]
    for reason in sorted(by_reason):
        lines.append("  {}: {}".format(reason, by_reason[reason]))
    lines.append("Ziffern with an unexplained difference: {}".format(len(left)))
    quarters = sorted({row[0] for row in left})
    if quarters:
        lines.append("quarters with an unexplained difference: {} ({})".format(len(quarters), ", ".join(quarters)))
    lines.append("the differences are rounding and the journal mirrors, see finance/docs/swiss.md")
    return lines


def erpnext_reports(directory):
    """The payment-basis report CSVs of a directory, sorted."""
    return sorted(glob.glob(os.path.join(directory, "mwst-*-payment.csv")))


def main(argv):
    parser = argparse.ArgumentParser(
        description="bexio's payment-basis Ziffern against ERPNext's, per quarter (read only).")
    parser.add_argument("--bexio", default=os.path.join(im.PRIVATE, "bexio-mwst-ziffern.csv"))
    parser.add_argument("--reports", default=im.PRIVATE,
                        help="the directory holding mwst-<quarter>-payment.csv (default <private>)")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-mwst-ziffern-compare.csv"))
    args = parser.parse_args(argv)

    bexio = read_bexio(args.bexio)
    erpnext = read_erpnext(erpnext_reports(args.reports))
    rows = compare(bexio, erpnext)
    for line in summary_lines(rows):
        print(line)
    write_csv(args.report, rows)
    print("written: {}".format(args.report))
    return 1 if unexplained(rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""bexio's MWST Ziffern into ERPNext: one row per quarter and Ziffer in the doctype 'MWST bexio Ziffer'.

Reads the private CSV of mwst_bexio.py (bexio-mwst-ziffern.csv: quarter, ziffer, entgelt_chf, steuer_chf,
...) and upserts the rows through the REST API (import_master's Erp, the API user), so the report
'MWST pro Quartal' can join bexio's declared figures by quarter and Ziffer. The doctype's name is
MWST-BEXIO-<quarter>-<ziffer>, so a rerun finds its rows and changes nothing — idempotent, as the
master-data import is.

Dry run by default: it reads and counts, writes nothing. --write performs the live write, once, with
ledgerdemain's go. The stdout shows counts, never amounts: this repo is public.

Rows of a quarter bexio has no figures for are simply not in the CSV; the report shows the blanks. The
CSV's journal and tolerance columns are not loaded: the doctype holds what bexio declared, the compare
script (mwst_ziffern_compare.py) owns the tolerance.

Run it as:

    python3 finance/bexio/load_mwst_bexio.py [--report FILE] [--write] [--source TEXT]

Standard library only, plus import_master.
"""

import argparse
import collections
import csv
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal

import import_master as im

DOCTYPE = "MWST bexio Ziffer"
CENT = Decimal("0.01")
QUARTER = re.compile(r"\d{4}Q[1-4]$")
ZIFFER = re.compile(r"\d{1,3}$")


def money(text):
    """A CSV amount as a cent Decimal; an empty cell is zero, as a Ziffer without Entgelt has."""
    return Decimal(text or "0").quantize(CENT, rounding=ROUND_HALF_UP)


def name_of(quarter, ziffer):
    """The row's name, as the doctype's autoname builds it (format:MWST-BEXIO-{quarter}-{ziffer})."""
    return "MWST-BEXIO-{}-{}".format(quarter, ziffer)


def read_rows(path):
    """[(quarter, ziffer, base, tax)] of the CSV, the totals per row (not its journal part). A row that is
    not a quarter with a Ziffer number stops the run: a broken CSV must not be loaded piece by piece."""
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for line in csv.DictReader(f):
            quarter, ziffer = line["quarter"].strip(), line["ziffer"].strip()
            if not (QUARTER.match(quarter) and ZIFFER.match(ziffer)):
                raise ValueError("row {!r}/{!r}: expected 2026Q3 and a Ziffer number".format(quarter, ziffer))
            rows.append((quarter, ziffer, money(line["entgelt_chf"]), money(line["steuer_chf"])))
    return rows


def load(erp, rows, source, dry_run=True):
    """Upsert the rows; returns the counts (created, updated, unchanged, failed). Rows equal to what the
    site has are left untouched; a live run writes only the differences."""
    counts = collections.Counter()
    for quarter, ziffer, base, tax in rows:
        name = name_of(quarter, ziffer)
        want = {"quarter": quarter, "ziffer": ziffer, "base": float(base), "tax": float(tax), "source": source}
        try:
            known = {row["name"] for row in erp.list(DOCTYPE)}
            if name in known:
                have = erp.get(DOCTYPE, name)
                if im.differs(have, want):
                    if not dry_run:
                        erp.update(DOCTYPE, name, want)
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
            else:
                if not dry_run:
                    erp.insert(DOCTYPE, dict(want, doctype=DOCTYPE))
                counts["created"] += 1
        except im.ErpError as err:
            print("failed: {} (HTTP {})".format(name, err.status))
            counts["failed"] += 1
    return counts


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-mwst-ziffern.csv"),
                        help="the private CSV of mwst_bexio.py (default <private>/bexio-mwst-ziffern.csv)")
    parser.add_argument("--write", action="store_true", help="write to ERPNext (default: dry run, counts only)")
    parser.add_argument("--source", default="bexio export 2026-10-10",
                        help="the Quelle stamped on the rows when they change or are created")
    args = parser.parse_args(argv)

    rows = read_rows(args.report)
    quarters = sorted({quarter for quarter, _z, _b, _t in rows})
    counts = load(im.Erp.from_file(), rows, args.source, dry_run=not args.write)
    print("quarters: {}, rows: {}".format(len(quarters), len(rows)))
    print("dry run, nothing written" if not args.write else "written")
    print("created: {}, updated: {}, unchanged: {}, failed: {}".format(
        counts["created"], counts["updated"], counts["unchanged"], counts["failed"]))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

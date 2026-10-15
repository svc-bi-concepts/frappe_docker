"""Count the documents the bexio import chain writes, per doctype: the ones keyed by bexio_id.

The before/after figure of a delta run (finance/bexio/delta.sh) and the rerun check of the
import chain (finance/bexio/README.md) as a command. Only documents that carry a bexio_id are
counted, so the Wise and PayPal syncs, the payment runs and the HR data do not move the
figure. Nothing is written; stdout carries one "doctype<TAB>count" line per doctype.

    python3 finance/bexio/delta_counts.py

Standard library only, like the importers.
"""

import sys

import import_master as im

DOCTYPES = (
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
    "Bank Transaction",
    "File",
)


def main(argv):
    erp = im.Erp.from_file(im.TOKEN_FILE)
    for doctype in DOCTYPES:
        rows = erp.list(doctype, [["bexio_id", "is", "set"]], ["name"])
        print(f"{doctype}\t{len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

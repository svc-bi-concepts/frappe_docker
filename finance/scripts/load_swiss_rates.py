"""Load the insurer rates from the private rates CSV into Payroll Swiss Settings.

The rates live in private/payroll-insurance/rates-<year>.csv (mode 600, never in the
repo). The host part reads it and emits the rows to set as JSON; the container part
receives them in RATES_JSON and writes the settings single. Two modes, one file:

    host:      python3 load_swiss_rates.py --prepare <rates.csv>     (JSON on stdout)
    container: RATES_JSON='[...]' python - --apply|--dry             (script on stdin)

Only rows with set=yes are applied, and only fields in RATE_FIELDS (the same list
swiss_rates.py carries; test_load_swiss_rates.py guards the agreement). Which fields
a run sets is the CSV's call: a rate whose sheet has not landed stays set=no and
keeps its placeholder. No rate is printed: the output names fields and whether a
value changed, the values themselves are read back on the site.
"""

import argparse
import csv
import json
import os
import sys

# The settings' rate fields, as bi_payroll/bi_payroll/swiss_rates.py's RATE_FIELDS.
# The test asserts the two lists agree, so a new field cannot be added on one side only.
RATE_FIELDS = [
    "swiss_ahv_ee",
    "swiss_ahv_ag",
    "swiss_alv_ee",
    "swiss_alv_ag",
    "swiss_alv_ceiling",
    "swiss_bvg_threshold",
    "swiss_bvg_coordination",
    "swiss_bvg_ee_25",
    "swiss_bvg_ee_35",
    "swiss_bvg_ee_45",
    "swiss_bvg_ee_55",
    "swiss_bvg_ag_25",
    "swiss_bvg_ag_35",
    "swiss_bvg_ag_45",
    "swiss_bvg_ag_55",
    "swiss_uvg_max",
    "swiss_uvg_ag",
    "swiss_nbu_ee",
    "swiss_ktg_ee",
    "swiss_ktg_ag",
    "swiss_fak_ag",
]

SETTINGS = "Payroll Swiss Settings"


def prepare(csv_path):
    """The rows the CSV sets: [{field, value}] for set=yes, floats, known fields only.

    Rows with set=no are documentation (waiting period, caps, the second rate column)
    and are skipped. A rate that is not a number or a field outside RATE_FIELDS stops
    the run: the CSV is written by hand, so a typo must not reach the settings.
    """
    rows = []
    with open(csv_path, newline="") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            if (row.get("set") or "").strip().lower() != "yes":
                continue
            field = (row.get("field") or "").strip()
            if field not in RATE_FIELDS:
                raise ValueError(
                    "line %d: field %r is not a settings rate field" % (line, field)
                )
            try:
                value = float((row.get("value") or "").strip())
            except ValueError:
                raise ValueError(
                    "line %d: value %r is not a number" % (line, row.get("value"))
                )
            rows.append({"field": field, "value": value})
    if not rows:
        raise ValueError("no row with set=yes in %s" % csv_path)
    return rows


def container_apply(dry):
    """Set the settings single from RATES_JSON, or report what would change. No rate on stdout."""
    import frappe

    rows = json.loads(os.environ["RATES_JSON"])
    unknown = [r["field"] for r in rows if r["field"] not in RATE_FIELDS]
    if unknown:
        raise SystemExit("refused: not settings rate fields: %s" % ", ".join(unknown))
    frappe.init(site="frontend")
    frappe.connect()
    doc = frappe.get_doc(SETTINGS)
    changed = []
    for row in rows:
        old = doc.get(row["field"])
        if old is None or float(old) != row["value"]:
            changed.append(row["field"])
            doc.set(row["field"], row["value"])
    if dry:
        print(
            "%s: %d of %d fields would change (%s), nothing saved"
            % (SETTINGS, len(changed), len(rows), ", ".join(changed) or "none")
        )
    else:
        doc.save()
        frappe.db.commit()
        print(
            "%s: %d of %d fields changed (%s)"
            % (SETTINGS, len(changed), len(rows), ", ".join(changed) or "none")
        )
    frappe.destroy()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare", metavar="RATES_CSV", help="host: the rates CSV, rows as JSON"
    )
    parser.add_argument(
        "--apply", action="store_true", help="container: save the settings"
    )
    parser.add_argument(
        "--dry", action="store_true", help="container: report without saving"
    )
    args = parser.parse_args(argv)
    if args.prepare:
        print(json.dumps(prepare(args.prepare)))
        return 0
    if args.apply or args.dry:
        if "frappe" not in sys.modules:
            try:
                import frappe  # noqa: F401
            except ImportError:
                raise SystemExit(
                    "refused: no frappe here; --apply runs in the backend container"
                )
        container_apply(dry=args.dry)
        return 0
    parser.error(
        "nothing to do: --prepare <csv> on the host, or --apply/--dry in the container"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

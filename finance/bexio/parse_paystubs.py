"""Parse the bexio paystub PDFs into one private figures table.

private/bexio-payroll/paystubs/ holds one PDF per employee and month (export.py --payroll). This runs
pdftotext -layout on each (on the host, no docker, no live call) and takes every wage-type line bexio
prints with its number: 1000/1001 monthly salary, 5010 AHV/IV/EO, 5020 ALV, 5040 NBU, 5050 BVG/PP,
5060 withholding tax, any other 5xxx deduction, plus the totals (5000 gross, 5900 total deductions,
6500 net, 6600 paid out), with base, rate and amount where the line prints them (Swiss apostrophes,
deductions negative). The figures go to private/payroll/paystub-figures.csv (one row per wage-type
line per paystub), the per-file problems to private/payroll/paystub-figures-report.txt; both mode 600,
both outside the repository. Payslips are the most sensitive documents of the company: no figure, name
or line of a real paystub reaches the repo, a commit or a note — stdout here is counts only, and the
test fixture is an invented text in bexio's layout (fixtures/paystub-invented*.txt).

    python3 finance/bexio/parse_paystubs.py [--check] [--paystubs-json PATH] [--paystubs-dir PATH]
                                            [--out CSV] [--report TXT] [--pdftotext BIN]

The layout has two shapes: named sections ("Lohn und Zulagen", "Abzüge", "Sonstige Zulagen und
Abzüge") and bare column header lines ("Lohnart ... Betrag CHF" / "Wage type ... Amount CHF") before
each block. Only lines after the first header count: the addresses above the table start with postal
codes that look like wage-type numbers. --check verifies per file that the printed 5xxx deductions sum
to gross minus net within 5 rappen; failures are listed in the report and counted on stdout, and with
--check the script exits 2. Standard library only.
"""

import argparse
import collections
import csv
import json
import os
import re
import subprocess
import sys

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"
PAYSTUBS_JSON = os.path.join(PRIVATE, "bexio-payroll", "paystubs.json")
PAYSTUBS_DIR = os.path.join(PRIVATE, "bexio-payroll", "paystubs")
OUT_CSV = os.path.join(PRIVATE, "payroll", "paystub-figures.csv")
REPORT = os.path.join(PRIVATE, "payroll", "paystub-figures-report.txt")
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"

CHECK_TOLERANCE = 0.05  # 5 rappen: bexio rounds every printed line to the cent
TOTAL_TYPES = {"5000": "gross", "5900": "total deductions", "6500": "net", "6600": "paid out"}
# Lines whose absence the report lists per file: the monthly salary (1000 or 1001), and the totals.
# 5010/5020 are on nearly every slip but a correction slip can lack them; 5040/5050/5060 apply per
# employee only (pension plan, withholding tax), so their absence is no problem.
EXPECTED_TYPES = ("5000", "5010", "5020", "5900", "6500")
SALARY_TYPES = ("1000", "1001")

COLUMN_HEADER = re.compile(r"^(?:Lohnart|Wage type)\b.*CHF$")
SECTION_HEADER = re.compile(r"^(?:Lohn und Zulagen|Abzüge|Sonstige Zulagen und Abzüge)\s*$")
WAGE_LINE = re.compile(r"^(\d{4}(?:\.\d)?)\s+(\S.*?)\s{2,}(\S.*?)$")
NUMBER = re.compile(r"-?\d[\d']*(?:\.\d+)?%?")

CSV_COLUMNS = ["employee_id", "year", "month", "wage_type", "label", "base", "rate", "amount"]


def to_number(token):
    """The float of one printed figure: the Swiss thousands apostrophes out, a trailing % off."""
    return float(token.rstrip("%").replace("'", ""))


def parse_line(line):
    """One wage-type line as (wage_type, label, base, rate, amount), or None when it is no wage line.

    The tail after the label holds the figures: the amount last, the rate marked with %, the base
    first when the line prints one beside the amount (a 5060 correction prints base and amount, no
    rate; the 1000 salary prints rate and amount, no base). A negative deduction keeps its sign.
    """
    match = WAGE_LINE.match(line)
    if not match:
        return None
    wage_type, label, tail = match.groups()
    tokens = NUMBER.findall(tail)
    if not tokens:
        return None
    rates = [to_number(token) for token in tokens if token.endswith("%")]
    figures = [to_number(token) for token in tokens if not token.endswith("%")]
    base = figures[0] if len(figures) > 1 else None
    rate = rates[0] if rates else None
    amount = figures[-1] if figures else None
    return wage_type, label.strip(), base, rate, amount


def parse_text(text):
    """The wage-type lines of one paystub text, in print order.

    Only lines after the first column or section header count: the addresses above the table start
    with postal codes that look like wage-type numbers.
    """
    rows = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if COLUMN_HEADER.match(line) or SECTION_HEADER.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        parsed = parse_line(line)
        if parsed:
            rows.append(parsed)
    return rows


def check_file(rows):
    """'ok', 'fail' or 'not_checkable': the 5xxx deductions sum to gross minus net within 5 rappen.

    The deduction amounts are negative as printed (a correction may be positive), so their sum must
    be the negative of gross minus net.
    """
    gross = net = None
    deductions = 0.0
    for wage_type, _, _, _, amount in rows:
        if wage_type == "5000":
            gross = amount
        elif wage_type == "6500":
            net = amount
        elif wage_type.startswith("5") and wage_type not in ("5000", "5900") and amount is not None:
            deductions += amount
    if gross is None or net is None:
        return "not_checkable"
    return "ok" if abs(gross - net + deductions) <= CHECK_TOLERANCE else "fail"


def pdftotext_text(pdftotext, path):
    """The -layout text of one PDF, on stdout (a scratch, never a file in a worktree)."""
    result = subprocess.run([pdftotext, "-layout", path, "-"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed on {os.path.basename(path)}: {result.stderr.strip()}")
    return result.stdout


def parse_records(records, paystubs_dir, pdftotext=PDFTOTEXT):
    """The CSV rows of all paystubs, the per-file facts and the counts.

    rows: one dict per wage-type line, with the employee, year and month from the record; per_file:
    {file name: {"types": [..], "problems": [..], "check": result}} — the file names hold employee
    ids, so per_file goes to the private report only; counts: the stdout summary, counts only;
    per_type: lines seen per wage-type number.
    """
    rows, per_file = [], {}
    counts = collections.Counter(files=len(records))
    per_type = collections.Counter()
    for record in records:
        name = record["file"]
        text = pdftotext_text(pdftotext, os.path.join(paystubs_dir, name))
        parsed = parse_text(text)
        types = [row[0] for row in parsed]
        problems = []
        if not text.strip():
            counts["empty_text"] += 1
            problems.append("pdftotext printed nothing (a scanned PDF?)")
        if parsed:
            counts["parsed"] += 1
        per_type.update(types)
        if not any(t in SALARY_TYPES for t in types):
            counts["no_salary"] += 1
            problems.append("no monthly salary line (1000/1001)")
        for expected in EXPECTED_TYPES:
            if expected not in types:
                problems.append(f"no {expected} line")
        result = check_file(parsed)
        if result == "fail":
            counts["check_failed"] += 1
            problems.append("the deductions do not sum to gross minus net within 5 rappen")
        elif result == "not_checkable":
            problems.append("gross or net not printed: not checked")
        per_file[name] = {"types": types, "problems": problems, "check": result}
        for wage_type, label, base, rate, amount in parsed:
            rows.append({
                "employee_id": record["employee_id"],
                "year": record["year"],
                "month": record["month"],
                "wage_type": wage_type,
                "label": label,
                "base": base,
                "rate": rate,
                "amount": amount,
            })
    counts["rows"] = len(rows)
    return rows, per_file, counts, per_type


def _write_private(path, write):
    """Write into private/: the parent directory 700, the file 600, both outside the repository."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    with open(path, "w", encoding="utf-8", newline="") as f:
        write(f)
    os.chmod(path, 0o600)


def _csv_figure(value, column):
    """A figure for the CSV: money to two decimals, the rate as printed precision (%g)."""
    if value is None:
        return ""
    return f"{value:.2f}" if column in ("base", "amount") else f"{value:g}"


def write_csv(path, rows):
    def write(f):
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([row["employee_id"], row["year"], row["month"], row["wage_type"], row["label"]]
                            + [_csv_figure(row[column], column) for column in ("base", "rate", "amount")])
    _write_private(path, write)


def write_report(path, per_file, counts, per_type):
    def write(f):
        f.write("bexio paystub figures report: stdout carries the counts, this file the per-file facts.\n")
        f.write(f"files: {counts['files']}  parsed: {counts['parsed']}  pdftotext empty: {counts['empty_text']}"
                f"  rows: {counts['rows']}  checks failed: {counts['check_failed']}\n")
        f.write("lines per wage type: "
                + ", ".join(f"{wage_type} {per_type[wage_type]}" for wage_type in sorted(per_type)) + "\n")
        flagged = [name for name, facts in per_file.items() if facts["problems"]]
        f.write(f"files without problems: {len(per_file) - len(flagged)}\n\n")
        for name in flagged:
            facts = per_file[name]
            f.write(f"{name}\n")
            for problem in facts["problems"]:
                f.write(f"    {problem}\n")
            f.write(f"    check: {facts['check']}\n")
    _write_private(path, write)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 2 when a file's deductions do not sum to gross minus net within 5 rappen")
    parser.add_argument("--paystubs-json", default=PAYSTUBS_JSON)
    parser.add_argument("--paystubs-dir", default=PAYSTUBS_DIR)
    parser.add_argument("--out", default=OUT_CSV)
    parser.add_argument("--report", default=REPORT)
    parser.add_argument("--pdftotext", default=PDFTOTEXT)
    args = parser.parse_args(argv)

    with open(args.paystubs_json, encoding="utf-8") as f:
        records = json.load(f)
    rows, per_file, counts, per_type = parse_records(records, args.paystubs_dir, args.pdftotext)
    write_csv(args.out, rows)
    write_report(args.report, per_file, counts, per_type)

    print(f"files: {counts['files']}")
    print(f"parsed: {counts['parsed']}")
    print(f"pdftotext empty (scanned PDFs): {counts['empty_text']}")
    print(f"wage-type rows: {counts['rows']}")
    print("lines per wage type: "
          + ", ".join(f"{wage_type} {per_type[wage_type]}" for wage_type in sorted(per_type)))
    print(f"files without a monthly salary line (1000/1001): {counts['no_salary']}")
    print(f"checks failed: {counts['check_failed']}")
    print(f"csv: {args.out} (600)")
    print(f"report: {args.report} (600, the per-file problems there)")
    if args.check and counts["check_failed"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Report the figures of one Swiss VAT period (effective method) from ERPNext, by bexio VAT code.

Reads the submitted Sales and Purchase Invoices of a period from ERPNext, one
row per tax template and per invoice, and adds them up by bexio's 42 VAT codes
and by the Ziffern of the MWST form. Nothing is written to ERPNext: the report
reads, and writes one CSV file under <private>. The stdout shows totals only.

A tax row finds its template by the row's description, which the importers set
to the template's name, and the template by its bexio_id. A row whose template
or code is not known is reported as unmapped, never guessed from the rate.

Two bases. The posting basis (default) counts an invoice in the period of its
posting date. The payment basis (--basis payment, the effective method on
vereinnahmte Entgelte) counts each Payment Entry in the period of its posting
date, and each invoice it pays with the share allocated / grand total: the
invoice's net and tax rows times that share, so a receipt is split over the
rates of the invoices it pays. Payment Entries that pay no invoice (advances,
bank charges) split no tax and are counted apart.

Journal Entries count on both bases by their posting date: the VAT rows of the
direct bank and card entries (1170, 1171), the sales tax booked by hand (2200,
with its base from the net row of the same voucher) and the Bezugsteuer (2203).
The journal part selects by the voucher's bexio_id key, not by account pair: a
voucher keyed vatfix-invoice-, vatfix-credit- or vatfix-bill- (the mirror of a
document-date posting) is never counted, a voucher keyed manual-<id> or
vatfix-manual-<id> counts every VAT row — its 2200 row rates from the entry
group's base row when the voucher's own rows hold only the difference — the
payment-day moves (keyed by bexio journal line id) count by the account pairs
except the Bezugsteuer move 2202 -> 2203, which the Payment Entries' shared
deduct rows already hold, and any other voucher counts by the account pairs and
is listed; the settlement (a voucher with 2201) stays out. See
journal_totals().

The base of a tax row is derived from its tax (tax / rate), because the import
does not carry the net per row. At 8.1 % one rappen of tax is about 0.06 of
base, so the derived bases are approximate; the tax per row is exact.

Ziffern: the table TEMPLATES (bi_finance's mwst_payment.py, shared with the VAT
Declaration's fill) is the form's row per code. The rows the ESTV form confirmed
for the tax carry a Ziffer (302/303, 312/313, 342/343, 382/383, 399, 400, 405,
410, 415, 420, 479), and so do the deductions of Ziffer 200 that a zero-rate sales
code fills: UEX 220, ULA 221, MEL 225. A document's zero-rate net is its net less
the derived bases of its rated rows, and goes whole to its code's row. The codes
with no deduction row (UNO, SUB, SPE, U00) carry None; their net is listed in the
private CSV and the VAT Declaration's fill refuses the period over it, naming
them. 235 stays 0 (no code fills it) and the Abzüge (280, 289, 299) are the
form's: erpnextswiss's vat_declaration.js computes 289 and 299 from the fields on
save. On the form 420 is Vorsteuerkürzungen (reductions for subsidies and the
like; none known, so it stays 0) and the total of the Vorsteuern is 479 = 400 +
405 + 410 + 415 - 420, not 420. See the note of erp-agpf for the open points.

Run it as:

    python3 finance/bexio/mwst_report.py --period 2026Q3

Standard library only, plus import_master for the ERPNext client.
"""

import argparse
import csv
import datetime
import os
import sys
from decimal import Decimal

import import_master as im

# The payment basis (rows, plan, journal part) is bi_finance's, shared with the VAT Declaration's fill (stdlib only).
# The names below are the module's; mwst_bexio and the tests read them through this module, so they stay importable here.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "apps", "bi_finance"
    ),
)
from bi_finance.mwst_payment import (  # noqa: E402
    INVOICE_DOCTYPES,
    TEMPLATES,
    Totals,
    declaration_fields,
    derived_base,
    form_totals,
    journal_totals,
    owed_row,
    payment_documents,
    period_bounds,
    plan,
    share_of,
)

COMPANY = im.COMPANY
ZERO = Decimal("0")


# The form rows that get a total, with a label for the output.
LABELS = {
    "200": "Gesamtbetrag der Entgelte (CHF, netto)",
    "302": "Normalsatz 7.7 %",
    "303": "Normalsatz 8.1 %",
    "312": "Reduzierter Satz 2.5 %",
    "313": "Reduzierter Satz 2.6 %",
    "342": "Sondersatz 3.7 %",
    "343": "Sondersatz 3.8 %",
    "382": "Bezugsteuer (bis 2023)",
    "383": "Bezugsteuer (ab 2024)",
    "399": "Total der geschuldeten Steuer",
    "400": "Vorsteuer Material- und Dienstleistungsaufwand",
    "405": "Vorsteuer Investitionen und übriger Betriebsaufwand",
    "410": "Verminderung der Vorsteuer (veränderte Verwendung)",
    "415": "Vorsteuerkorrekturen",
    "420": "Vorsteuerkürzungen",
    "479": "Total der Vorsteuern",
    "220": "Ausfuhren",
    "221": "Übrige steuerfreie Leistungen",
    "225": "Meldepflichtige Verfahren (Übergang des Steueranspruchs)",
    "230": "Von der Steuer ausgenommene Umsätze",
    "235": "Minderung des Entgelts",
}


def csv_rows(totals):
    """One line per form row, base and tax, and under it the part that came from Journal Entries, for the private CSV."""
    rows, _saldo = form_totals(totals)
    for row in sorted(set(rows) | set(LABELS)):
        if row == "200" or row in rows:
            base, tax = rows.get(row, [ZERO, ZERO])
            yield [row, LABELS.get(row, row), f"{base:.2f}", f"{tax:.2f}"]
            if row in totals.journal:
                jbase, jtax = totals.journal[row]
                yield [
                    row,
                    "journal entries (by account)",
                    f"{jbase:.2f}",
                    f"{jtax:.2f}",
                ]


def summary_lines(totals):
    """The stdout of a run: totals per form row and what is not in a checked row. No names, no bexio ids."""
    rows, saldo = form_totals(totals)
    lines = ["{:<6}{:<50}{:>16}{:>14}".format("Ziffer", "", "Entgelt", "Steuer")]
    for row in (
        "200",
        "220",
        "221",
        "225",
        "230",
        "235",
        "302",
        "303",
        "312",
        "313",
        "342",
        "343",
        "382",
        "383",
        "399",
        "400",
        "405",
        "410",
        "415",
        "420",
        "479",
    ):
        base, tax = rows.get(row, [ZERO, ZERO])
        if row == "200" or base or tax:
            lines.append(f"{row:<6}{LABELS[row]:<50}{base:>16,.2f}{tax:>14,.2f}")
    lines.append("")
    lines.append(
        f"Saldo before Ziffer 479 and Abzüge 280: {saldo:,.2f} (positive: to pay)"
    )
    lines.append(
        f"tax not in a checked Ziffer (imports and the sales codes with no deduction row): {totals.unchecked_tax:,.2f}"
    )
    lines.append(
        f"journal entry sales tax with no base row or no known rate, counted apart: {totals.rate_unknown:,.2f}"
    )
    nowhere = sum(totals.undeducted_net.values(), ZERO) + sum(
        totals.unsplit_net.values(), ZERO
    )
    if nowhere:
        lines.append(
            f"sales net with no deduction row: {nowhere:,.2f} (codes in the private CSV, the fill refuses over it)"
        )
    if totals.unmapped:
        lines.append(
            f"unmapped tax rows: {len(totals.unmapped)} (names in the private CSV's log, not here)"
        )
    return lines


def write_csv(path, totals):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ziffer", "label", "entgelt_chf", "steuer_chf"])
        for line in csv_rows(totals):
            writer.writerow(line)
        writer.writerow([])
        writer.writerow(["code", "steuer_chf"])
        for code in sorted(totals.by_code):
            writer.writerow([code, f"{totals.by_code[code][1]:.2f}"])
        writer.writerow([])
        writer.writerow(["unmapped_template", "rows"])
        for name, count in sorted(_count(totals.unmapped).items()):
            writer.writerow([name, count])
        writer.writerow([])
        writer.writerow(["skipped mirrors", "vouchers"])
        writer.writerow(["invoice/credit/bill vatfix-*", len(totals.skipped_mirrors)])
        writer.writerow([])
        writer.writerow(["skipped bezug moves", "vouchers"])
        writer.writerow(
            ["2202 -> 2203 keyed by journal line", len(totals.skipped_moves)]
        )
        writer.writerow([])
        writer.writerow(["other journal keys (pair rule)", "vouchers"])
        for key, count in sorted(_count(totals.other_keys).items()):
            writer.writerow([key, count])
        if totals.undeducted_net or totals.unsplit_net:
            writer.writerow([])
            writer.writerow(["code", "net with no deduction row"])
            for code, net in sorted(totals.undeducted_net.items()):
                writer.writerow([code, f"{net:.2f}"])
            for codes, net in sorted(totals.unsplit_net.items()):
                writer.writerow(["/".join(codes), f"{net:.2f}"])


def _count(items):
    out = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def fetch(erp, start, end):
    """The submitted invoices of the period and their tax rows, read through the API user."""
    filters = [
        ["company", "=", COMPANY],
        ["docstatus", "=", 1],
        ["posting_date", "between", [str(start), str(end)]],
    ]
    sales = erp.list(
        "Sales Invoice", filters, ["name", "posting_date", "base_net_total"]
    )
    purchases = erp.list(
        "Purchase Invoice", filters, ["name", "posting_date", "base_net_total"]
    )
    out = {}
    for kind, docs in (("sales", sales), ("purchases", purchases)):
        out[kind] = []
        for d in docs:
            doctype = "Sales Invoice" if kind == "sales" else "Purchase Invoice"
            full = erp.get(doctype, d["name"])
            out[kind].append(
                {
                    "posting_date": datetime.date.fromisoformat(
                        str(full["posting_date"])
                    ),
                    "base_net_total": full["base_net_total"],
                    "taxes": full.get("taxes") or [],
                }
            )
    return out


def fetch_payments(erp, start, end):
    """The submitted Payment Entries of the period with their references, and the invoices they pay (read only).
    Returns the payments, and (doctype, name) -> the invoice as ERPNext has it."""
    filters = [
        ["company", "=", COMPANY],
        ["docstatus", "=", 1],
        ["posting_date", "between", [str(start), str(end)]],
    ]
    payments, invoices = [], {}
    for p in erp.list("Payment Entry", filters, ["name", "posting_date"]):
        full = erp.get("Payment Entry", p["name"])
        references = full.get("references") or []
        for ref in references:
            key = (ref["reference_doctype"], ref["reference_name"])
            if ref["reference_doctype"] in INVOICE_DOCTYPES and key not in invoices:
                invoices[key] = erp.get(*key)
        payments.append(
            {
                "posting_date": datetime.date.fromisoformat(str(full["posting_date"])),
                "references": references,
            }
        )
    return payments, invoices


def fetch_journal_lines(erp, start, end):
    """The GL rows of the submitted Journal Entries of the period (read only): voucher, posting date, account
    number, and debit minus credit, each line with its voucher's bexio_id key ('key', None when it has none;
    journal_totals() selects by it). The rows of every account are read, the base of a sales tax is one of them.
    A row whose account is not in the company's chart comes back with account None."""
    numbers = {
        a["name"]: a["account_number"]
        for a in erp.list(
            "Account", [["company", "=", COMPANY]], ["name", "account_number"]
        )
    }
    keys = {
        e["name"]: (e.get("bexio_id") or None)
        for e in erp.list(
            "Journal Entry",
            [
                ["company", "=", COMPANY],
                ["docstatus", "=", 1],
                ["posting_date", "between", [str(start), str(end)]],
            ],
            ["name", "bexio_id"],
        )
    }
    filters = [
        ["company", "=", COMPANY],
        ["voucher_type", "=", "Journal Entry"],
        ["is_cancelled", "=", 0],
        ["posting_date", "between", [str(start), str(end)]],
    ]
    lines = []
    for e in erp.list(
        "GL Entry",
        filters,
        ["voucher_no", "posting_date", "account", "debit", "credit"],
    ):
        lines.append(
            {
                "voucher": e["voucher_no"],
                "posting_date": datetime.date.fromisoformat(str(e["posting_date"])),
                "account": numbers.get(e["account"]),
                "amount": Decimal(str(e["debit"])) - Decimal(str(e["credit"])),
                "key": keys.get(e["voucher_no"]),
            }
        )
    return lines


def templates_by_name(erp):
    """Tax template name -> bexio id, for the templates that have one (the sales and purchase ones)."""
    names = {}
    for doctype in (
        "Sales Taxes and Charges Template",
        "Purchase Taxes and Charges Template",
    ):
        for t in erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"]):
            names[t["name"]] = str(t["bexio_id"])
    return names


def write_fields(path, fields):
    """The VAT Declaration's fields of a period, one per line, sorted by name. Figures of the company: the file is private."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["field", "amount"])
        for name in sorted(fields):
            writer.writerow([name, f"{fields[name]:.2f}"])


def default_report(period, basis):
    """The private CSV of a run. The payment basis has its own file, so a run of one basis never overwrites the other."""
    name = f"mwst-{period}.csv" if basis == "posting" else f"mwst-{period}-{basis}.csv"
    return os.path.join(im.PRIVATE, name)


def main(argv):
    parser = argparse.ArgumentParser(
        description="MWST figures of one period by bexio VAT code, from ERPNext (read only)."
    )
    parser.add_argument(
        "--period", required=True, help="2026Q3 or 2026H1 (effective method)"
    )
    parser.add_argument(
        "--basis",
        choices=("posting", "payment"),
        default="posting",
        help="posting: invoices by posting date (default); payment: Payment Entries by payment date, "
        "each over the invoices it pays",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="private CSV (default <private>/mwst-<period>.csv)",
    )
    parser.add_argument(
        "--fields",
        default=None,
        help="payment basis only: also write the VAT Declaration's fields of the period to this private CSV, "
        "as the fill would set them (nothing is written to ERPNext)",
    )
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.fields and args.basis != "payment":
        parser.error(
            "--fields is for the payment basis, the one the VAT Declaration fill uses"
        )
    start, end = period_bounds(args.period)
    report = args.report or default_report(args.period, args.basis)

    erp = im.Erp.from_file(args.token_file)
    try:
        templates = templates_by_name(erp)
        if args.basis == "payment":
            payments, invoices = fetch_payments(erp, start, end)
        else:
            docs = fetch(erp, start, end)
        gl = gl_entries(erp, start, end)
        journal = fetch_journal_lines(erp, start, end)
    except im.ErpError as err:
        print(f"aborted: {err}", file=sys.stderr)
        return 2

    if args.basis == "payment":
        sales, purchases, unallocated = payment_documents(payments, invoices)
        totals = plan(sales, purchases, templates)
        print(
            "MWST {} ({} to {}, payment basis): {} payments, {} of them pay no invoice".format(
                args.period, start, end, len(payments), unallocated
            )
        )
    else:
        totals = plan(docs["sales"], docs["purchases"], templates)
        print(
            "MWST {} ({} to {}): {} sales and {} purchase invoices".format(
                args.period, start, end, len(docs["sales"]), len(docs["purchases"])
            )
        )
    journal_totals(journal, totals)
    for line in summary_lines(totals):
        print(line)
    print("")
    for line in reconcile_lines(totals, gl):
        print(line)
    write_csv(report, totals)
    print(f"written: {report}")
    if args.fields:
        try:
            fields = declaration_fields(totals)
        except ValueError as err:
            print(f"fields not written: {err}")
            return 1
        write_fields(args.fields, fields)
        print(f"written: {args.fields}")
    return 1 if totals.unmapped else 0


GL_ACCOUNTS = ("2200", "1170", "1171", "2201")


def gl_entries(erp, start, end):
    """The GL entries of the period on the VAT accounts 2200, 1170, 1171 and the settlement account 2201, one dict
    each with the account number, the voucher it posts from, debit and credit (read only). The checks below go by
    voucher, because bexio's settlement is a voucher of its own and the invoice rows are not.
    """
    accounts = erp.list(
        "Account",
        [["company", "=", COMPANY], ["account_number", "in", list(GL_ACCOUNTS)]],
        ["name", "account_number"],
    )
    names = {a["name"]: str(a["account_number"]) for a in accounts}
    if not names:
        return []
    entries = erp.list(
        "GL Entry",
        [
            ["account", "in", list(names)],
            ["is_cancelled", "=", 0],
            ["posting_date", "between", [str(start), str(end)]],
        ],
        ["account", "voucher_type", "voucher_no", "debit", "credit"],
    )
    return [
        {
            "number": names[e["account"]],
            "voucher_type": e["voucher_type"],
            "voucher_no": e["voucher_no"],
            "debit": Decimal(str(e["debit"])),
            "credit": Decimal(str(e["credit"])),
        }
        for e in entries
    ]


def gl_owed(gl):
    """Sales tax moved to 2200 in the period: the credits on 2200 from vouchers other than Sales Invoice. The
    Sales Invoice credits are left out, because the import's mirror entries reverse them to 2202 at the invoice date,
    so the credit of a sale and its reversal net out, and what remains is the receipt's move and manual sales tax.
    """
    return sum(
        (
            e["credit"]
            for e in gl
            if e["number"] == "2200" and e["voucher_type"] != "Sales Invoice"
        ),
        ZERO,
    )


def gl_deductible(gl):
    """Input tax moved to 1170 and 1171 in the period: the debits from vouchers other than Purchase Invoice, the
    same way as gl_owed. Purchase Invoice debits are left out for the same reason, bexio books them at the bill date.
    """
    return sum(
        (
            e["debit"]
            for e in gl
            if e["number"] in ("1170", "1171")
            and e["voucher_type"] != "Purchase Invoice"
        ),
        ZERO,
    )


def settlement(gl):
    """The settlement of the period: the vouchers that post against 2201, read for their 2200 debit (the sales tax
    declared) and their 1170 and 1171 credits (the input tax declared). bexio books it once per quarter; a voucher
    that only moves 2201 against the bank is not a settlement. Returns (settled, declared owed, declared input).
    """
    vouchers = {
        (e["voucher_type"], e["voucher_no"]) for e in gl if e["number"] == "2201"
    }
    owed = ZERO
    deductible = ZERO
    for e in gl:
        if (e["voucher_type"], e["voucher_no"]) not in vouchers:
            continue
        if e["number"] == "2200":
            owed += e["debit"]
        elif e["number"] in ("1170", "1171"):
            deductible += e["credit"]
    return bool(owed or deductible), owed, deductible


def reconcile_lines(totals, gl):
    """Sales tax (Ziffer 399) against the gross credits of 2200, Vorsteuer (479) against the gross debits of 1170 and
    1171, and the settlement of the period against both sums. The net of the accounts is zero in a settled quarter,
    so it is not what is checked here."""
    rows, _ = form_totals(totals)
    owed = rows.get("399", [ZERO, ZERO])[1]
    vorsteuer = rows.get("479", [ZERO, ZERO])[1]
    gl_sales = gl_owed(gl)
    gl_input = gl_deductible(gl)
    settled, declared_owed, declared_input = settlement(gl)
    lines = [
        f"check against GL: owed on 2200 {gl_sales:,.2f} vs Ziffer 399 {owed:,.2f}: difference {gl_sales - owed:,.2f}",
        f"check against GL: input tax on 1170+1171 {gl_input:,.2f} vs Ziffer 479 {vorsteuer:,.2f}: difference {gl_input - vorsteuer:,.2f}",
    ]
    if settled:
        lines.append(
            "settlement: settled: yes; 2200 debit {:,.2f} vs owed {:,.2f}: difference {:,.2f}; 1170+1171 credit {:,.2f} vs input tax {:,.2f}: difference {:,.2f}".format(
                declared_owed,
                gl_sales,
                declared_owed - gl_sales,
                declared_input,
                gl_input,
                declared_input - gl_input,
            )
        )
    else:
        lines.append("settlement: settled: no")
    lines.append(
        "differences come from manual journal entries with tax and from the bases not in a checked Ziffer"
    )
    return lines


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

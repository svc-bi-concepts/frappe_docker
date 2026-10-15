"""Bexio's MWST form figures per quarter, from the export alone, on the payment basis.

bexio declares on the effective method (vereinnahmte Entgelte): a sale's VAT is due in the
quarter of its receipt, a bill's input VAT in the quarter of its payment. This module builds
the figures of the form per quarter from the export's payments and documents, never from the
journal's VAT moves (the journal is what ERPNext imported, so journal against GL proves only
the import; see mwst_compare.py):

- an invoice payment adds the invoice's net and tax per VAT code times its share, the share
  being what the payment still settles of the invoice's total; a credit-voucher offset gives
  back what was paid before, with the sign; a payment past the total settles nothing (bexio
  books it as an advance and moves no VAT);
- a bill payment adds the whole bill's net and tax per VAT code on its payment day (bexio
  pairs every bill with one payment; the pairing is import_payments_out's, the same one the
  import used, so the ERPNext Payment Entries split the same way); a reverse-charge bill's
  tax goes into the Vorsteuer row of its code and into the Bezugsteuer row of its day;
- a manual entry adds its VAT by posting date, the amount being gross when the line has an
  ordinary code and net on a reverse-charge one, split as import_manual_entries splits it.

The code to Ziffer table is mwst_report's TEMPLATES, imported, so a mapping fix moves both
sides. A foreign-currency invoice's figures are taken at the CHF total bexio itself booked
(the receivable lines of its journal), which is what ERPNext holds since erp-h7dj.

The form rows are written to one private CSV with a quarter column, the journal part of each
row kept apart (as mwst_report's CSV keeps it) and a tolerance for the Entgelt: ERPNext
derives a document's base from its tax (the import carries no net per tax row), so the two
sides' bases may differ by up to the bound per contributing row, which the tolerance sums.
mwst_ziffern_compare.py reads this CSV against mwst_report.py --basis payment's. The stdout
shows counts only.

Run it as:

    python3 finance/bexio/mwst_bexio.py [--export DIR] [--report FILE] [--years]

Standard library only, plus import_master, import_purchase, import_payments_out,
import_manual_entries and mwst_report.
"""

import argparse
import collections
import csv
import datetime
import json
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

import import_manual_entries as ime
import import_master as im
import import_payments_out as ipo
import import_purchase as ip
import mwst_report as mr

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
BASE_CURRENCY_ID = 1
# the export's account types, as bexio means them (1020 bank is 3, 3200 Honorare is 1)
BEXIO_ROOT = {1: "Income", 2: "Expense", 3: "Asset", 4: "Liability", 5: "Equity"}
PROFIT_AND_LOSS = ("Income", "Expense")
# the side the VAT account naturally carries: Vorsteuer debited, Umsatzsteuer and Bezugsteuer credited
NATURAL_SIDE = {"S": "credit", "P": "debit", "BZ": "credit"}
# the derived-base bound of one contributing document row: the report may split bexio's gross
# into net and tax a cent away from bexio's own split, and derives the base from that tax, so
# the bound is the cent shift amplified by 100/rate, plus the row's own cent and the share slack
DERIVED_BASE_SLACK = ONE
# the bound of the tax of one contributing document row: the report may split bexio's gross a
# cent away from bexio's own split, ERPNext's Rounded Total may take the gross down a 5-rappen
# step and the payment settles the rounded total, and the share rounds: half a franc of bound
# is what these three can move the tax of one row
TAX_BOUND = Decimal("0.05")
# the zero-rate codes of the manual entries: no VAT split on them, as in the importer
MANUAL_ZERO_RATE_IDS = ime.MANUAL_ZERO_RATE_IDS


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def cent_bound(rate):
    """How far mwst_report's base may be off the true net of one row: the report derives the
    base from its tax, whose own bound (TAX_BOUND) the division amplifies by 100/rate, plus
    the row's own cent and the share slack."""
    return (
        TAX_BOUND * HUNDRED / Decimal(str(rate)) + Decimal("0.005") + DERIVED_BASE_SLACK
    )


class Quarters:
    """Per quarter the form rows, the journal part of each row, and the Entgelt tolerance.

    rows holds mwst_report's Totals per quarter, so both sides' CSVs hold the same shape;
    tol sums the per-row bounds of the document part, for the comparison of the bases.
    """

    def __init__(self):
        self.rows = {}
        self.tol = collections.defaultdict(lambda: ZERO)
        self.tax_tol = collections.defaultdict(lambda: ZERO)
        self.counts = collections.Counter()
        self.unknown = collections.defaultdict(list)

    def of(self, quarter):
        return self.rows.setdefault(quarter, mr.Totals())

    def unknown_list(self, name):
        """The list an unmapped thing of kind name is counted in, so the caller only appends."""
        return self.unknown[name]

    def add_document(self, quarter, row, base, tax, rate, share=ONE):
        """A document's row: the true base and the shared tax, and the tolerances of their
        comparison against the report's figures."""
        self.of(quarter).add(row, base, tax)
        self.tax_tol[(quarter, row)] += TAX_BOUND * share
        if base is not None:
            self.tol[(quarter, row)] += cent_bound(rate) * share

    def add_journal(self, quarter, row, base, tax):
        self.of(quarter).add_journal(row, base, tax)


def chf(value, rate):
    """An amount of a foreign document in CHF at the given rate, to the cent."""
    return value if rate == ONE else money(value * rate)


def numbers_of(accounts):
    """bexio account id -> account number, for the receivable account this reads."""
    return {
        a["id"]: str(a["account_no"])
        for a in accounts
        if str(a["account_no"]) == "1100"
    }


def booked_chf_of_invoices(journal, numbers):
    """The CHF total bexio booked for each invoice: the receivable (1100) lines of its journal
    booking, summed and rounded once. Only a foreign invoice needs it; its export total is in
    the invoice currency, and the CHF bexio booked is the one ERPNext holds."""
    out = collections.defaultdict(lambda: ZERO)
    for line in journal:
        if (
            line.get("ref_class") != "KbInvoice"
            or numbers.get(line["debit_account_id"]) != "1100"
        ):
            continue
        if line.get("ref_id") is not None:
            out[line["ref_id"]] += Decimal(str(line["base_currency_amount"]))
    return {invoice_id: money(total) for invoice_id, total in out.items()}


def invoice_codes(invoice, taxes, chf_booked=None, unknown=None):
    """The invoice's net and tax per bexio tax id in CHF, its untaxed net, and its total net.

    The net of a code is the sum of its positions' totals; a discount the export does not
    carry on a row of its own is spread over the codes in proportion, so the codes add up to
    the invoice's net. The tax of a code is its rate times that net, to the cent, with
    bexio's own per-rate totals (the taxs) as the authority: their value is shared over the
    codes of the rate in proportion, the rounding residual on the largest. A position whose
    tax id the export's taxes do not carry is counted in unknown and left out.
    """
    rate = ONE
    total_ccy = Decimal(str(invoice["total"]))
    if invoice.get("currency_id") != BASE_CURRENCY_ID:
        if chf_booked is None or not total_ccy:
            raise ValueError(
                "invoice {} in a foreign currency without its booked CHF total".format(
                    invoice["id"]
                )
            )
        rate = chf_booked / total_ccy
    total_net = chf(Decimal(str(invoice["total_net"])), rate)
    sums = collections.defaultdict(lambda: ZERO)
    for position in invoice.get("positions") or []:
        tax_id = position.get("tax_id")
        if tax_id is None:
            continue
        if tax_id not in taxes:
            if unknown is not None:
                unknown.append(tax_id)
            continue
        sums[tax_id] += Decimal(str(position.get("position_total") or 0))
    positions_total = sum(sums.values(), ZERO)
    if positions_total and positions_total != Decimal(str(invoice["total_net"])):
        factor = Decimal(str(invoice["total_net"])) / positions_total
        sums = {tax_id: net * factor for tax_id, net in sums.items()}
    codes = {tax_id: [chf(money(net), rate), ZERO] for tax_id, net in sums.items()}
    by_rate = collections.defaultdict(list)
    for tax_id in codes:
        rate_pct = Decimal(str(taxes[tax_id]["value"]))
        if rate_pct:
            by_rate[rate_pct].append(tax_id)
    for rate_pct, tax_ids in by_rate.items():
        given = ZERO
        for tax_line in invoice.get("taxs") or []:
            if Decimal(tax_line["percentage"]) == rate_pct:
                given += chf(Decimal(tax_line["value"]), rate)
        if not given:
            # bexio's own per-rate totals are the authority: a rate the invoice does not carry
            # (a position's code the invoice was booked without) taxes nothing
            continue
        net_total = sum(codes[tax_id][0] for tax_id in tax_ids)
        if not net_total:
            continue
        largest = max(tax_ids, key=lambda t: codes[t][0])
        computed = ZERO
        for tax_id in tax_ids:
            codes[tax_id][1] = (
                given
                if len(tax_ids) == 1
                else money(given * codes[tax_id][0] / net_total)
            )
            computed += codes[tax_id][1]
        if computed != given:
            codes[largest][1] += given - computed
    untaxed = total_net - sum(net for net, _tax in codes.values())
    return (
        {tax_id: (net, tax) for tax_id, (net, tax) in codes.items()},
        money(untaxed),
        total_net,
    )


def receipt_shares(rows, total):
    """What each payment row of one invoice settles, in date order: the rows take the total
    until it is paid, a credit-voucher offset gives back what was paid, with the sign, and a
    payment past the total settles nothing (an advance, bexio moves no VAT for it). A cash
    discount settles no VAT either. Yields (day, share, kind), kind 'payment' or 'credit'.
    """
    allocated = ZERO
    for row in sorted(rows, key=lambda r: (r["date"], r["id"])):
        if row.get("is_cash_discount"):
            continue
        value = money(row["value"])
        day = row["date"][:10]
        if row.get("kb_credit_voucher_id") is not None:
            taken = min(value, allocated)
            allocated -= taken
            if taken:
                yield day, -taken / total, "credit"
        else:
            taken = min(value, total - allocated)
            allocated += taken
            if taken:
                yield day, taken / total, "payment"


def quarter_of_day(day):
    """'2026-08-14' -> '2026Q3'."""
    return f"{day[:4]}Q{(int(day[5:7]) - 1) // 3 + 1}"


def add_sales(quarters, invoices, invoice_payments, taxes, chf_booked):
    """The receipt side: every invoice payment adds its invoice's net and tax per code times
    its share, in the quarter of the payment day. Ziffer 200 takes the whole shared net; a
    code without a checked row only adds its (zero) tax to the unchecked rest, like the
    report's plan() does."""
    for entry in invoice_payments:
        invoice = invoices.get(entry["parent_id"])
        rows = [
            row for row in entry.get("rows") or [] if not row.get("is_cash_discount")
        ]
        if invoice is None:
            quarters.counts["receipts without their invoice"] += len(rows)
            continue
        total = money(invoice["total"])
        if not total:
            quarters.counts["invoices without a total"] += 1
            continue
        codes, untaxed, total_net = invoice_codes(
            invoice,
            taxes,
            chf_booked.get(entry["parent_id"]),
            quarters.unknown_list("sales positions"),
        )
        for day, share, kind in receipt_shares(entry["rows"], total):
            totals = quarters.of(quarter_of_day(day))
            quarters.counts["receipts counted"] += 1
            if kind == "credit":
                quarters.counts["credit-voucher offsets counted"] += 1
            quarters.tax_tol[(quarter_of_day(day), "200")] += TAX_BOUND * share
            quarters.tol[(quarter_of_day(day), "200")] += TAX_BOUND * share
            totals.add("200", mr.share_of(total_net, share), ZERO)
            for tax_id, (net, tax) in codes.items():
                template = mr.TEMPLATES.get(tax_id)
                if template is None:
                    quarters.counts["sales lines on an unknown code"] += 1
                    continue
                _code, rate_pct, _kind, form_row = template
                if form_row is None:
                    totals.unchecked_tax += mr.share_of(tax, share)
                    continue
                if not tax:
                    # the report books no tax row for a rate the invoice does not carry, so its
                    # net is in Ziffer 200 only, as here
                    continue
                quarters.add_document(
                    quarter_of_day(day),
                    form_row,
                    mr.share_of(net, share),
                    mr.share_of(tax, share),
                    rate_pct,
                    share,
                )


def bill_codes(bill, taxes, unknown=None):
    """The bill's net and tax per bexio tax id in CHF, as import_purchase maps it: a line's
    VAT is the one bexio gives (tax_calc) or its rate times the line's net, a foreign bill's
    lines convert at bexio's rate."""
    currency = bill["currency_code"]
    rate = ONE if currency == ip.BASE_CURRENCY else Decimal(str(bill["exchange_rate"]))
    codes = collections.defaultdict(lambda: [ZERO, ZERO])
    for line in ip._lines_of(bill):
        tax_id = line.get("tax_id")
        if tax_id is None:
            continue  # an untaxed line: its net is in no Ziffer on the report's side either
        if tax_id not in taxes:
            if unknown is not None:
                unknown.append(tax_id)
            continue
        rate_pct = Decimal(str(taxes[tax_id]["value"]))
        net = chf(line["net"], rate)
        codes[tax_id][0] += net
        if rate_pct:
            given = (
                line["given"]
                if line["given"] is not None
                else ip._tax_of(line["net"], rate_pct)
            )
            codes[tax_id][1] += chf(given, rate)
    return {tax_id: (money(net), money(tax)) for tax_id, (net, tax) in codes.items()}


def add_purchase(quarters, bills, journal, accounts, taxes):
    """The bill-payment side: import_payments_out's pairing of the journal's payment groups
    with the bills, and each pair adds the whole bill's net and tax per code in the quarter
    of the payment. A reverse-charge bill's tax also goes into the Bezugsteuer row of the
    payment day, as bexio moves it 2202 -> 2203 there and the report's Deduct row does.
    """
    numbers = {a["id"]: str(a["account_no"]) for a in accounts}
    booked = ipo.bill_bookings(journal, numbers)
    settled = []
    for uuid, lines in ipo.groups_of(journal).items():
        try:
            settled.append(ipo.settle(uuid, lines, numbers))
        except ipo.Unmapped:
            quarters.counts["bill payments not settled"] += 1
    bill_dates = {bill["id"]: bill["bill_date"] for bill in bills}
    pairs, unpaired, open_bills = ipo.pair(settled, booked, bill_dates)
    quarters.counts["bill payments counted"] = len(pairs)
    quarters.counts["bill payments without their bill"] = len(unpaired)
    quarters.counts["bills without a payment"] = len(open_bills)
    by_id = {bill["id"]: bill for bill in bills}
    for item, bill_uuid in pairs:
        bill = by_id.get(bill_uuid)
        if bill is None:
            quarters.counts["bill payments without their bill"] += 1
            continue
        quarter = quarter_of_day(item["day"])
        try:
            codes = bill_codes(bill, taxes, quarters.unknown_list("bill lines"))
        except ip.MappingError:
            quarters.counts["bills whose lines do not map"] += 1
            continue
        for tax_id, (net, tax) in codes.items():
            template = mr.TEMPLATES.get(tax_id) if tax_id is not None else None
            if template is None:
                quarters.counts["bill lines on an unknown code"] += 1
                continue
            _code, rate_pct, kind, form_row = template
            if form_row is None:
                quarters.of(quarter).unchecked_tax += tax
                continue
            if kind == "BZ":
                # the report's Deduct row carries a base too, derived from the tax; bexio's is
                # the bill's net, so the comparison holds it against the same tolerance
                quarters.add_document(quarter, form_row, net, tax, rate_pct)
                quarters.add_document(
                    quarter,
                    mr.owed_row(datetime.date.fromisoformat(item["day"])),
                    net,
                    tax,
                    rate_pct,
                )
            else:
                # a code at 0 % has no base on the report's side either (its derived base is None)
                quarters.add_document(
                    quarter, form_row, net if rate_pct else None, tax, rate_pct
                )


def vat_side(line, roots, kind):
    """The side the VAT of a manual-entry line is booked on, as import_manual_entries decides
    it: the side of the income or expense account of the line, else the VAT account's own. A
    reverse-charge line's VAT is the Vorsteuer of its kind, so its fallback is that side.
    """
    debit_root = roots.get(line.get("debit_account_id"))
    credit_root = roots.get(line.get("credit_account_id"))
    if debit_root in PROFIT_AND_LOSS and credit_root not in PROFIT_AND_LOSS:
        return "debit"
    if credit_root in PROFIT_AND_LOSS and debit_root not in PROFIT_AND_LOSS:
        return "credit"
    return NATURAL_SIDE["P" if kind == "BZ" else kind]


def add_manual(quarters, manual_entries, accounts, taxes):
    """The manual entries' VAT by posting date, split as the import splits it: the amount of
    a line with an ordinary code is gross and the VAT is the rest of the gross over the net;
    a reverse-charge line is net, and its tax goes into the Vorsteuer row of its code and is
    taken back on the Bezugsteuer row of the day. The side rule of vat_side decides the sign,
    so a reversal counts negative."""
    roots = {a["id"]: BEXIO_ROOT.get(a.get("account_type")) for a in accounts}
    for entry in manual_entries:
        day = entry["date"][:10]
        quarter = quarter_of_day(day)
        for line in entry.get("entries") or []:
            tax_id = line.get("tax_id")
            if not tax_id or tax_id in MANUAL_ZERO_RATE_IDS:
                continue
            template = mr.TEMPLATES.get(tax_id)
            if template is None:
                quarters.counts["manual lines on an unknown code"] += 1
                continue
            _code, rate_pct, kind, form_row = template
            factor = Decimal(str(line.get("currency_factor") or 1))
            amount = Decimal(str(line["amount"]))
            side = vat_side(line, roots, kind)
            sign = ONE if side == NATURAL_SIDE[kind] else -ONE
            if kind == "BZ":
                # the amount is net; the tax is booked on the Vorsteuer and taken back on 2203,
                # both in the direction of the expense or income side of the line
                tax = money(money(amount * Decimal(str(rate_pct)) / HUNDRED) * factor)
                sign = ONE if side == "debit" else -ONE
                quarters.add_journal(
                    quarter, form_row, chf(amount, factor) * sign, tax * sign
                )
                quarters.add_journal(
                    quarter,
                    mr.owed_row(datetime.date.fromisoformat(day)),
                    None,
                    tax * sign,
                )
                quarters.counts["manual reverse-charge lines"] += 1
                continue
            net = money(amount / (ONE + Decimal(str(rate_pct)) / HUNDRED))
            tax = money(amount * factor) - money(net * factor)
            if not tax:
                continue
            quarters.add_journal(
                quarter, form_row, money(net * factor) * sign, tax * sign
            )
            quarters.counts["manual lines with VAT"] += 1


def quarters_of(days):
    """The quarters from the earliest to the latest of the given dates ('2026-08-14')."""
    if not days:
        return []

    def quarter_of(day):
        return (int(day[:4]), (int(day[5:7]) - 1) // 3 + 1)

    start, end = quarter_of(min(days)), quarter_of(max(days))
    out, year, q = [], start[0], start[1]
    while (year, q) <= end:
        out.append(f"{year}Q{q}")
        q += 1
        if q == 5:
            year, q = year + 1, 1
    return out


def years_without_receipt_moves(journal, accounts):
    """The years of the journal whose journal has no 2202 -> 2200 move: such a year was
    declared on the invoice basis or had no taxed sales, and its quarters are compared on the
    posting basis instead, to confirm with Benchi."""
    numbers = {a["id"]: str(a["account_no"]) for a in accounts}
    years_with = {
        line["date"][:4]
        for line in journal
        if (
            numbers.get(line["debit_account_id"]),
            numbers.get(line["credit_account_id"]),
        )
        == ("2202", "2200")
    }
    years = {line["date"][:4] for line in journal}
    return sorted(years - years_with)


TOTAL_ROWS = {
    "399": ("302", "303", "312", "313", "342", "343", "382", "383"),
    "479": ("400", "405", "410", "415", "420"),
}


def write_csv(path, quarters, order):
    """One line per quarter and form row with base, tax, the journal part and the tolerances.
    The totals 399 and 479 carry the sums of their components' journal parts and tolerances,
    so a difference of a total names its parts the same way the rows do."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "quarter",
                "ziffer",
                "label",
                "entgelt_chf",
                "steuer_chf",
                "journal_entgelt_chf",
                "journal_steuer_chf",
                "entgelt_tol_chf",
                "steuer_tol_chf",
            ]
        )
        for quarter in order:
            totals = quarters.rows.get(quarter)
            if totals is None:
                continue
            rows, _saldo = mr.form_totals(totals)
            for row in sorted(set(rows) | set(totals.journal)):
                base, tax = rows.get(row, [ZERO, ZERO])
                jbase, jtax = totals.journal.get(row, [ZERO, ZERO])
                tol = quarters.tol.get((quarter, row), ZERO)
                stol = quarters.tax_tol.get((quarter, row), ZERO)
                for part in TOTAL_ROWS.get(
                    row, ()
                ):  # a total's journal tax and tolerance are its parts' sums
                    _pbase, ptax = totals.journal.get(part, [ZERO, ZERO])
                    jtax += ptax
                    tol += quarters.tol.get((quarter, part), ZERO)
                    stol += quarters.tax_tol.get((quarter, part), ZERO)
                if not (base or tax or jbase or jtax):
                    continue
                writer.writerow(
                    [
                        quarter,
                        row,
                        mr.LABELS.get(row, row),
                        f"{base:.2f}",
                        f"{tax:.2f}",
                        f"{jbase:.2f}",
                        f"{jtax:.2f}",
                        f"{tol:.2f}",
                        f"{stol:.2f}",
                    ]
                )


def summary_lines(quarters):
    """The stdout: counts only, no amounts."""
    return [
        f"quarters with rows: {len(quarters.rows)}",
        "receipts: {} counted, {} credit-voucher offsets, {} without their invoice".format(
            quarters.counts["receipts counted"],
            quarters.counts["credit-voucher offsets counted"],
            quarters.counts["receipts without their invoice"],
        ),
        "bill payments: {} counted, {} without their bill, {} not settled, {} bills without a payment".format(
            quarters.counts["bill payments counted"],
            quarters.counts["bill payments without their bill"],
            quarters.counts["bill payments not settled"],
            quarters.counts["bills without a payment"],
        ),
        "manual lines: {} with VAT, {} reverse-charge, {} on an unknown code".format(
            quarters.counts["manual lines with VAT"],
            quarters.counts["manual reverse-charge lines"],
            quarters.counts["manual lines on an unknown code"],
        ),
        "unknown codes: {} sales positions, {} bill lines, {} bills whose lines do not map".format(
            len(quarters.unknown["sales positions"]),
            len(quarters.unknown["bill lines"]),
            quarters.counts["bills whose lines do not map"],
        ),
    ]


def load_export(export_dir):
    """The export files this reads, by name."""
    out = {}
    for name in (
        "invoices",
        "invoice_payments",
        "bills",
        "journal",
        "manual_entries",
        "taxes",
        "accounts",
    ):
        with open(os.path.join(export_dir, name + ".json"), encoding="utf-8") as f:
            out[name] = json.load(f)
    return out


def build(data):
    """The quarters of the whole export: sales, purchases and manual entries."""
    taxes = {t["id"]: t for t in data["taxes"]}
    invoices = {i["id"]: i for i in data["invoices"]}
    chf_booked = booked_chf_of_invoices(data["journal"], numbers_of(data["accounts"]))
    quarters = Quarters()
    add_sales(quarters, invoices, data["invoice_payments"], taxes, chf_booked)
    add_purchase(quarters, data["bills"], data["journal"], data["accounts"], taxes)
    add_manual(quarters, data["manual_entries"], data["accounts"], taxes)
    return quarters


def main(argv):
    parser = argparse.ArgumentParser(
        description="bexio's MWST figures per quarter from the export, payment basis (read only)."
    )
    parser.add_argument(
        "--export",
        default=im.newest_export(),
        help="the export directory (default the newest under <private>/bexio-export)",
    )
    parser.add_argument(
        "--report", default=os.path.join(im.PRIVATE, "bexio-mwst-ziffern.csv")
    )
    parser.add_argument(
        "--years",
        action="store_true",
        help="list the years without a 2202 -> 2200 move and stop",
    )
    args = parser.parse_args(argv)
    data = load_export(args.export)
    if args.years:
        for year in years_without_receipt_moves(data["journal"], data["accounts"]):
            print(year)
        return 0
    quarters = build(data)
    for line in summary_lines(quarters):
        print(line)
    write_csv(
        args.report,
        quarters,
        quarters_of([line["date"][:10] for line in data["journal"]]),
    )
    print(f"written: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

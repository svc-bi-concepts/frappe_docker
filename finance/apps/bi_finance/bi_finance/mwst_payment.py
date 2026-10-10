"""The payment basis of the MWST figures: one stdlib module, two callers, no frappe and no network.

mwst_report.py (on the host, over the REST API) and the VAT Declaration's fill (vat_declaration.py, over frappe.db) read
the same rows and build the same Totals from them: the Payment Entries of a period over the invoices they pay
(payment_documents), their tax rows by bexio VAT code (plan), and the Journal Entries' VAT (journal_totals). Only the
reading of the rows differs, and each caller does its own. declaration_fields() turns the Totals into the VAT
Declaration's fields. The rules of the basis (the mirrors, the moves, the manual entries) are in journal_totals() and in
the module note of mwst_report.py.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")

# The Bezugsteuer owed moved from Ziffer 382 to 383 with the rate change; the form has one row each.
# Date of the change as the form gives it (the bexio validity column is not used for this).
BEZUG_NEW_ROW_FROM = datetime.date(2024, 1, 1)

# Optiert (UO77, UO81) is taxed at the normal rate, so its tax is owed in 302/303 like UN77/UN81.
# bexio VAT id -> (code, rate %, kind, form row). Kind S = sales, P = purchase, BZ = Bezugsteuer.
# The form row is where the tax of the template's Add row goes; for BZ the Deduct row
# (the reverse charge on 2203) is the owed tax, see owed_row(). None = not in a checked Ziffer.
# The ids and codes are those of swiss-setup.py's BEXIO_TAXES; the test checks that they match.
TEMPLATES = {
    16: ("UN77", 7.7, "S", "302"),
    28: ("UN81", 8.1, "S", "303"),
    17: ("UR25", 2.5, "S", "312"),
    29: ("UR26", 2.6, "S", "313"),
    18: ("US37", 3.7, "S", "342"),
    30: ("US38", 3.8, "S", "343"),
    3: ("UEX", 0, "S", None),
    4: ("ULA", 0, "S", None),
    5: ("MEL", 0, "S", None),
    6: ("UNO", 0, "S", None),
    13: ("SUB", 0, "S", None),
    14: ("SPE", 0, "S", None),
    15: ("UO77", 7.7, "S", "302"),
    31: ("UO81", 8.1, "S", "303"),
    48: ("U00", 0, "S", None),
    22: ("VM77", 7.7, "P", "400"),
    35: ("VM81", 8.1, "P", "400"),
    8: ("VM25", 2.5, "P", "400"),
    34: ("VM26", 2.6, "P", "400"),
    21: ("VM37", 3.7, "P", "400"),
    36: ("VM38", 3.8, "P", "400"),
    7: ("VIM", 0, "P", None),
    9: ("ZOLLM", 0, "P", None),
    33: ("BZM81", 8.1, "BZ", "400"),
    19: ("BZM77", 7.7, "BZ", "400"),
    24: ("VB77", 7.7, "P", "405"),
    38: ("VB81", 8.1, "P", "405"),
    12: ("VB25", 2.5, "P", "405"),
    37: ("VB26", 2.6, "P", "405"),
    23: ("VB37", 3.7, "P", "405"),
    39: ("VB38", 3.8, "P", "405"),
    47: ("V00", 0, "P", "405"),
    10: ("VSF", 0, "P", None),
    11: ("ZOLLB", 0, "P", None),
    32: ("BZB81", 8.1, "BZ", "405"),
    20: ("BZB77", 7.7, "BZ", "405"),
    25: ("VES", 7.7, "P", "415"),
    26: ("VEV", 7.7, "P", "415"),
    27: ("VKÜ", 7.7, "P", "415"),
    40: ("VES", 8.1, "P", "415"),
    41: ("VEV", 8.1, "P", "415"),
    42: ("VKÜ", 8.1, "P", "415"),
}

def period_bounds(period):
    """'2026Q3' is a quarter, '2026H1' or '2026H2' a half-year: the first and last day as dates."""
    year, part = int(period[:4]), period[4:]
    if part in ("Q1", "Q2", "Q3", "Q4"):
        first_month = 3 * (int(part[1]) - 1) + 1
        last_month = first_month + 2
    elif part in ("H1", "H2"):
        first_month = 1 if part == "H1" else 7
        last_month = first_month + 5
    else:
        raise ValueError("period {!r}: use 2026Q3 or 2026H1".format(period))
    first = datetime.date(year, first_month, 1)
    nxt = datetime.date(year + (last_month == 12), last_month % 12 + 1, 1)
    return first, nxt - datetime.timedelta(days=1)

def owed_row(posting_date):
    """The Ziffer of Bezugsteuer owed on a document of this date."""
    return "383" if posting_date >= BEZUG_NEW_ROW_FROM else "382"

def derived_base(tax, rate):
    """The net of a tax row from its tax: exact only to a few rappen, see the module note."""
    if not rate:
        return None
    return (tax * 100 / Decimal(str(rate))).quantize(CENT, rounding=ROUND_HALF_UP)

class Totals:
    """Base and tax per form row, and what could not be put in a row."""

    def __init__(self):
        self.rows = {}
        self.unmapped = []
        self.unchecked_tax = ZERO
        self.by_code = {}
        self.journal = {}
        self.rate_unknown = ZERO
        self.skipped_mirrors = []   # the bexio_id keys of the vatfix-* vouchers the journal part skips
        self.skipped_moves = []     # the keys of the 2202 -> 2203 moves, which the Payment Entries' shared deduct rows hold
        self.other_keys = []        # the keys (or '(no bexio_id)') of vouchers the pair rule counts

    def add(self, row, base, tax):
        acc = self.rows.setdefault(row, [ZERO, ZERO])
        if base is not None:
            acc[0] += base
        acc[1] += tax

    def add_journal(self, row, base, tax):
        """A tax from a Journal Entry: in the row like a document's, and kept apart for the CSV's entries line."""
        self.add(row, base, tax)
        acc = self.journal.setdefault(row, [ZERO, ZERO])
        if base is not None:
            acc[0] += base
        acc[1] += tax

    def tax_of(self, row):
        return self.rows.get(row, [ZERO, ZERO])[1]

    def base_of(self, row):
        return self.rows.get(row, [ZERO, ZERO])[0]

def plan(sales, purchases, templates, totals=None):
    """Add the documents up by form row. sales and purchases are ERPNext documents as dicts:
    posting_date (date), base_net_total, and taxes (rows with description, rate, base_tax_amount,
    tax_amount, add_deduct_tax). templates maps a template name to its bexio id (string).
    Returns the Totals; unmapped rows are named by their template only."""
    totals = totals or Totals()
    for doc in sales:
        totals.add("200", Decimal(str(doc["base_net_total"])), ZERO)
        for row in doc["taxes"]:
            _tax_row(totals, row, templates, doc["posting_date"], purchase=False)
    for doc in purchases:
        for row in doc["taxes"]:
            _tax_row(totals, row, templates, doc["posting_date"], purchase=True)
    return totals

def _tax_row(totals, row, templates, posting_date, purchase):
    bexio_id = templates.get(row.get("description"))
    entry = TEMPLATES.get(int(bexio_id)) if bexio_id and bexio_id.isdigit() else None
    if entry is None:
        totals.unmapped.append(row.get("description") or "(no template)")
        return
    code, rate, kind, form_row = entry
    tax = Decimal(str(row.get("base_tax_amount") or row.get("tax_amount") or 0))
    deduct = purchase and row.get("add_deduct_tax") == "Deduct"
    if deduct:
        # a reverse charge comes back on 2203 as the owed tax; the same amount is the Vorsteuer on the Add row
        if kind != "BZ":
            totals.unmapped.append("Deduct row on {}".format(code))
            return
        form_row = owed_row(posting_date)
    totals.by_code.setdefault(code, [ZERO, ZERO])[1] += tax
    if form_row is None:
        totals.unchecked_tax += tax
        return
    totals.add(form_row, derived_base(tax, rate), tax)

def form_totals(totals):
    """The totals of the form rows that the form adds up: 399 owed and 479, the total of the Vorsteuern
    (400 + 405 + 410 + 415 - 420; 420 is Vorsteuerkürzungen on the form and stays 0, as 410 does,
    until a reduction row exists). 500 and 510 need data not in bexio."""
    owed = sum((totals.tax_of(r) for r in ("302", "303", "312", "313", "342", "343", "382", "383")), ZERO)
    vorsteuer = sum((totals.tax_of(r) for r in ("400", "405", "410", "415")), ZERO) - totals.tax_of("420")
    out = dict(totals.rows)
    out["399"] = [ZERO, owed]
    out["479"] = [ZERO, vorsteuer]
    return out, owed - vorsteuer

INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")

def share_of(amount, share):
    """An amount of an invoice cut to the share one payment pays of it, to the rappen."""
    return (Decimal(str(amount)) * share).quantize(CENT, rounding=ROUND_HALF_UP)

def payment_documents(payments, invoices):
    """The payment basis: each reference of a Payment Entry to an invoice as a document for plan().

    The share is allocated / the invoice's grand total. ERPNext gives the allocated amount the sign of the invoice:
    a refund against a credit note is negative, as the credit note is, so its share is positive and the credit
    note's tax comes out negative as it should. The share times the invoice's net and each tax row is one document
    dated by the payment; plan() adds it like a posted invoice. Exchange differences and deductions on a payment
    are not in the allocated amount and do not change the share.
    Returns the sales documents, the purchase documents, and the count of payments that pay no invoice."""
    sales, purchases = [], []
    unallocated = 0
    for pay in payments:
        refs = [r for r in pay["references"] if r["reference_doctype"] in INVOICE_DOCTYPES]
        if not refs:
            unallocated += 1
        for ref in refs:
            inv = invoices[(ref["reference_doctype"], ref["reference_name"])]
            if not inv["grand_total"]:
                raise ValueError("{} {} has no grand total: its payment share is not defined".format(
                    ref["reference_doctype"], ref["reference_name"]))
            share = Decimal(str(ref["allocated_amount"])) / Decimal(str(inv["grand_total"]))
            taxes = []
            for row in inv["taxes"]:
                tax = share_of(row.get("base_tax_amount") or row.get("tax_amount") or 0, share)
                taxes.append(dict(row, base_tax_amount=tax, tax_amount=tax))
            doc = {
                "posting_date": pay["posting_date"],
                "base_net_total": share_of(inv["base_net_total"], share),
                "taxes": taxes,
            }
            (sales if ref["reference_doctype"] == "Sales Invoice" else purchases).append(doc)
    return sales, purchases, unallocated

# Journal Entries: the input tax accounts and their Ziffer; 2203 (Bezugsteuer owed) goes by owed_row().
JOURNAL_INPUT = {"1170": "400", "1171": "405"}
# The accounts whose rows are tax, not the base: a sales tax's base is a row of another account.
VAT_ACCOUNTS = {"1170", "1171", "1172", "2200", "2201", "2202", "2203"}
# The sales rates of the form rows. A rate is matched to the row within the tolerance: tax and base are rounded to the rappen.
RATE_ROWS = {Decimal("7.7"): "302", Decimal("8.1"): "303", Decimal("2.5"): "312",
             Decimal("2.6"): "313", Decimal("3.7"): "342", Decimal("3.8"): "343"}
RATE_TOLERANCE = Decimal("0.05")
# The keys of the document-date mirrors: a vatfix voucher of an invoice, a credit note or a bill books the VAT on the
# document's date, which the payment basis does not read — the payment-day moves are vouchers keyed by journal line.
# vatfix-manual- is not here: it corrects a bexio manual entry on the entry's posting date, which both bases read.
MIRROR_KEYS = ("vatfix-invoice-", "vatfix-credit-", "vatfix-bill-")

def _rate_row(rate):
    for known, row in RATE_ROWS.items():
        if abs(rate - known) <= RATE_TOLERANCE:
            return row
    return None

def _manual_group(key):
    """The bexio entry id of a manual key: manual-1099, a correction manual-1099-<suffix> and the
    VAT fix vatfix-manual-1099 are one group (import_manual_fix.group_of). None for any other key."""
    for prefix in ("vatfix-manual-", "manual-"):
        if key and key.startswith(prefix):
            return key[len(prefix):].split("-")[0]
    return None

def _manual_group_rate(sales_tax, key, vouchers):
    """The form row and base of a manual sales tax the voucher's own rows cannot rate, from its group.

    import_manual_entries books a manual entry's VAT as its own row on the entry's base account, and
    import_manual_fix moves it to 2200 in a vatfix-manual voucher of its own whose rows are only the
    difference, so the rate is not on the voucher. The group's manual-<id> voucher holds the base next
    to the VAT on the same account: the rate is the tax over that row. Exactly one row of the group
    may rate the tax, else (None, None) and the tax stays counted apart."""
    group = _manual_group(key)
    if group is None:
        return None, None
    matches = []
    for rows in vouchers.values():
        other = rows[0].get("key") or ""
        if other == key or not other.startswith("manual-") or _manual_group(other) != group:
            continue
        for line in rows:
            if line["account"] in VAT_ACCOUNTS:
                continue
            base = -line["amount"]
            if base and base * sales_tax > 0 and abs(base) != abs(sales_tax):
                matched = _rate_row(sales_tax * 100 / base)
                if matched is not None:
                    matches.append((matched, base))
    if len(set(matches)) == 1:
        return matches[0]
    return None, None

def journal_totals(lines, totals):
    """Journal Entries' VAT rows into the Ziffern, by posting date: the same on both bases (a bank or card entry is
    dated by the payment). fetch_journal_lines() gives the lines, each with its voucher's bexio_id key.

    Per voucher, by the accounts it touches. The settlement stays out (a voucher with 2201, whose 1171 row is the
    period's moves and direct lines in one). The rest decides by key, not by account pair: a voucher keyed
    vatfix-invoice-, vatfix-credit- or vatfix-bill- is the mirror of a document-date posting — the payment basis
    reads the payment-day moves instead, which are vouchers of their own keyed by bexio journal line id — and is
    skipped, counted in the CSV's skipped mirrors. A voucher keyed manual-<id> is a bexio manual entry, and a
    vatfix-manual-<id> voucher is the VAT fix of one: both hold the entry's VAT on the entry's posting date, so
    every VAT row counts (1170 -> 400, 1171 -> 405, 2203 -> 382/383 by the Bezugsteuer rule, 2200 by the net credit
    in the row of its rate); a 2202 or 1172 row is a reverse-charge transit, not a Ziffer. A manual 2200 row the
    voucher's own rows cannot rate rates from the entry's group (the base row the import booked next to the VAT,
    see _manual_group_rate). A move (keyed by journal line id) and any other voucher count by the account pairs
    as before: a 1170/1171 row with 1172 and a 2200 row with 2202 are the moves the Payment Entries already hold,
    not counted; 2200 alone is the sales tax booked by hand. The Bezugsteuer move (a numeric-keyed voucher's
    2203 row next to its 2202 debit) is the tax the Payment Entries' shared deduct rows already hold: not counted,
    listed in the CSV's skipped moves; a 2203 row on a voucher of no other known kind stays uncounted too and is
    listed, until its origin is named. A voucher of no known kind (or with no key) is listed in the CSV, so a new
    voucher kind shows. The rate comes from the one non-VAT row of the voucher with the same sign (the net); a
    sales tax without it, or at a rate not in the form, is counted apart in rate_unknown."""
    vouchers = {}
    for line in lines:
        if line["account"] is not None:
            vouchers.setdefault(line["voucher"], []).append(line)
    for rows in vouchers.values():
        net = {}
        for line in rows:
            net[line["account"]] = net.get(line["account"], ZERO) + line["amount"]
        if "2201" in net:
            continue
        key = rows[0].get("key")
        if key and key.startswith(MIRROR_KEYS):
            totals.skipped_mirrors.append(key)
            continue
        manual = bool(key) and (key.startswith("manual-") or key.startswith("vatfix-manual-"))
        # beyond the manual entries, the known keyed kinds are the payment-day moves: a bare number is
        # 2202 -> 2200 or 1172 -> 1170/1171, and with 2202 -> 2203 it is the Bezugsteuer move
        bezug_move = not manual and bool(key) and key.isdigit() and net.get("2203") and "2202" in net
        if not manual and not bezug_move and (not (key and key.isdigit()) or net.get("2203")) \
                and any(account in VAT_ACCOUNTS for account in net):
            totals.other_keys.append(key or "(no bexio_id)")
        posting_date = rows[0]["posting_date"]
        for account, row in JOURNAL_INPUT.items():
            if net.get(account) and (manual or "1172" not in net):
                totals.add_journal(row, None, net[account])
        if net.get("2203"):
            if manual:
                totals.add_journal(owed_row(posting_date), None, -net["2203"])
            elif bezug_move:
                totals.skipped_moves.append(key)
        if net.get("2200") and (manual or "2202" not in net):
            sales_tax = -net["2200"]
            bases = [-amount for account, amount in net.items()
                     if account not in VAT_ACCOUNTS and -amount * sales_tax > 0]
            row = _rate_row(sales_tax * 100 / bases[0]) if len(bases) == 1 else None
            base = bases[0] if len(bases) == 1 else None
            if row is None and manual:
                row, base = _manual_group_rate(sales_tax, key, vouchers)
            if row is None:
                totals.rate_unknown += sales_tax
            else:
                totals.add_journal(row, base, sales_tax)


# The VAT Declaration's fields by Ziffer: erpnextswiss's create_transfer_file maps them this way, the XML reads them.
FIELD_BY_ZIFFER = {
    "200": "total_revenue",
    "220": "tax_free_services",
    "221": "revenue_abroad",
    "225": "transfers",
    "230": "non_taxable_services",
    "235": "losses",
    "302": "normal_amount",
    "303": "normal_amount_2024",
    "312": "reduced_amount",
    "313": "reduced_amount_2024",
    "342": "lodging_amount",
    "343": "lodging_amount_2024",
    "382": "additional_amount",
    "383": "additional_amount_2024",
    "400": "pretax_material",
    "405": "pretax_investments",
    "410": "missing_pretax",
    "415": "pretax_correction_mixed",
    "420": "pretax_correction_other",
    "500": "payable_tax",
    "900": "grants",
    "910": "donations",
}

# The rate rows of the form: Ziffer, base field, rate field, tax field, rate in percent. The form computes a row's tax as
# base times rate (erpnextswiss's vat_declaration.js, update_tax_amounts), so the fill does the same.
RATE_FIELDS = [
    ("302", "normal_amount", "normal_rate", "normal_tax", "7.7"),
    ("303", "normal_amount_2024", "normal_rate_2024", "normal_tax_2024", "8.1"),
    ("312", "reduced_amount", "reduced_rate", "reduced_tax", "2.5"),
    ("313", "reduced_amount_2024", "reduced_rate_2024", "reduced_tax_2024", "2.6"),
    ("342", "lodging_amount", "lodging_rate", "lodging_tax", "3.7"),
    ("343", "lodging_amount_2024", "lodging_rate_2024", "lodging_tax_2024", "3.8"),
]
# Bezugsteuer: the base is the field, the tax is the tax as booked (the form takes it from the view, no rate field).
ACQUISITION_FIELDS = [("382", "additional_amount", "additional_tax"),
                      ("383", "additional_amount_2024", "additional_tax_2024")]
# Ziffern the report does not decide. A tax on one of them is refused, not dropped: the form's figure for them is open.
UNDECIDED_ZIFFERN = ("410", "415", "420")


def declaration_fields(totals):
    """The fields of one VAT Declaration for the period, by fieldname, as Decimals.

    Refuses (ValueError) what the fill cannot put right: a tax row no template maps, since the declaration would miss
    its tax, and a tax on an undecided Ziffer (UNDECIDED_ZIFFERN). Unchecked rows (Umsatz 220 to 299, zero-rate and
    imports) are not in the fields; the caller reports totals.unchecked_tax. The totals are the form's own: 399 is
    the sum of the tax rows, 479 the Vorsteuer (400 + 405), 500 what is payable (399 less 479)."""
    if totals.unmapped:
        raise ValueError("{} tax rows have no known template, so the declaration would miss their tax".format(
            len(totals.unmapped)))
    for ziffer in UNDECIDED_ZIFFERN:
        if totals.tax_of(ziffer):
            raise ValueError("Ziffer {} carries tax, which the fill does not set yet".format(ziffer))
    rows, _ = form_totals(totals)

    def base(ziffer):
        return rows.get(ziffer, [ZERO, ZERO])[0]

    def tax(ziffer):
        return rows.get(ziffer, [ZERO, ZERO])[1]

    fields = {"total_revenue": base("200")}
    total_tax = ZERO
    for ziffer, amount_field, rate_field, tax_field, rate in RATE_FIELDS:
        rate = Decimal(rate)
        row_tax = (base(ziffer) * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        fields[amount_field] = base(ziffer)
        fields[rate_field] = rate
        fields[tax_field] = row_tax
        total_tax += row_tax
    for ziffer, amount_field, tax_field in ACQUISITION_FIELDS:
        fields[amount_field] = base(ziffer)
        fields[tax_field] = tax(ziffer)
        total_tax += tax(ziffer)
    fields["pretax_material"] = tax("400")
    fields["pretax_investments"] = tax("405")
    for field in ("missing_pretax", "pretax_correction_mixed", "pretax_correction_other"):
        fields[field] = ZERO
    pretax = fields["pretax_material"] + fields["pretax_investments"]
    fields["total_tax"] = total_tax
    fields["total_pretax_reductions"] = pretax
    fields["payable_tax"] = total_tax - pretax
    return fields

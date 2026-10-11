"""The Swiss payslip (Lohnabrechnung) as data: the header, the rows and the totals the print format renders.

Pure Python, so the offline tests run without frappe (test_payslip.py). The template
(lohnabrechnung.html) reads it through the jinja method payslip_data (swiss_payroll.py), which
fetches the Employee and the Salary Structure Assignment the slip does not embed. The rows carry
the slip's own amounts and the rates copied onto it (swiss_payroll.copy_rates_to_slip), never the
settings' current values. German labels: the payslip is German (the bead's decision); the
customer-language rule of the QR slip does not apply. Nothing here names a real employee or rate.
"""

from datetime import date, datetime

# The known Salary Detail components per block: (component name, German label, rate field on the
# slip, base kind). The base kinds mirror the formulas in fixtures/salary_component.json: AHV, KTG
# and FAK sit on the full gross, ALV and NBU/UVG on the gross capped by their monthly ceiling,
# BVG on the monthly coordinated salary of the assignment. The rate field {age} is the slip's
# age-group rate, resolved with the age the BVG Age component computes (age at the period's end).
DEDUCTIONS = (
    ("AHV/IV/EO Employee", "AHV/IV/EO", "swiss_ahv_ee", "gross"),
    ("ALV Employee", "ALV", "swiss_alv_ee", "alv_ceiling"),
    ("BVG Employee", "BVG", "swiss_bvg_ee_{age}", "bvg"),
    ("NBU Employee", "NBU", "swiss_nbu_ee", "uvg_max"),
    ("KTG Employee", "KTG", "swiss_ktg_ee", "gross"),
    ("Quellensteuer Employee", "Quellensteuer", None, "gross"),
)
EMPLOYER = (
    ("AHV/IV/EO Employer", "AHV/IV/EO", "swiss_ahv_ag", "gross"),
    ("ALV Employer", "ALV", "swiss_alv_ag", "alv_ceiling"),
    ("BVG Employer", "BVG", "swiss_bvg_ag_{age}", "bvg"),
    ("UVG Employer", "UVG", "swiss_uvg_ag", "uvg_max"),
    ("KTG Employer", "KTG", "swiss_ktg_ag", "gross"),
    ("FAK Employer", "FAK", "swiss_fak_ag", "gross"),
)
EARNING_LABELS = {"Basic Salary": "Grundlohn", "Commission": "Provision"}
# Components that count into the year-to-date net: the employee's own costs. The employer rows and
# the BVG Age helper row (its amount is an age, not money) stay out.
NET_YTD_COMPONENTS = {d[0] for d in DEDUCTIONS}


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def money(v):
    """Swiss format: apostrophe between the thousands, two decimals ('1'234.50'); empty for None."""
    if v is None:
        return ""
    return "{:,.2f}".format(float(v)).replace(",", "'")


def pct(v):
    """A rate in percent with up to two decimals ('5.3 %', '0.44 %'); empty for None."""
    if v is None:
        return ""
    return "{:.2f}".format(float(v)).rstrip("0").rstrip(".") + " %"


def bvg_age_group(slip, employee, end_date=None):
    """The BVG age group key ('25', '35', '45', '55') or None: the same age the BVG Age component
    computes (years completed at the month and day of the period's end), then the group its
    formula picks (fixtures/salary_component.json). None outside the contribution window."""
    born = _date(_value(employee, "date_of_birth") if employee else None)
    end = _date(end_date)
    if not born or not end:
        return None
    age = end.year - born.year - ((end.month, end.day) < (born.month, born.day))
    if not 25 <= age < 65:
        return None
    if age < 35:
        return "25"
    if age < 45:
        return "35"
    if age < 55:
        return "45"
    return "55"


def _base(slip, assignment, kind, group):
    # The base the formula uses, monthly, in the slip's own numbers. 0 where the formula gives 0
    # (outside the BVG contribution window, or an insured salary under the entry threshold).
    gross = _num(_value(slip, "gross_pay"))
    if kind == "gross":
        return gross
    if kind == "alv_ceiling":
        return min(gross, _num(_value(slip, "swiss_alv_ceiling")) / 12)
    if kind == "uvg_max":
        return min(gross, _num(_value(slip, "swiss_uvg_max")) / 12)
    if kind == "bvg":
        insured = _num(_value(assignment, "swiss_bvg_insured_salary")) if assignment else 0.0
        if not group or insured < _num(_value(slip, "swiss_bvg_threshold")):
            return 0.0
        return max(0.0, insured - _num(_value(slip, "swiss_bvg_coordination"))) / 12
    return 0.0


def _rate(slip, rate_field, group):
    # The rate copied onto the slip; for BVG the age group's. None where the formula has none.
    if not rate_field:
        return None
    # only the BVG rates depend on the age group: AHV, ALV, NBU, KTG and the others apply at any age
    if "{age}" in rate_field and not group:
        return None
    return _num(_value(slip, rate_field.format(age=group)))


def _qst_label(employee):
    canton = _value(employee, "swiss_qst_canton") if employee else ""
    code = _value(employee, "swiss_qst_code") if employee else ""
    where = ", ".join(str(v) for v in (canton, code) if v)
    return f"Quellensteuer ({where})" if where else "Quellensteuer"


def _rows(lines, definitions, slip, employee, assignment, group, known_only=False, skip=()):
    # The rows the block prints, in the slip's own order, plus the block's amount total. The
    # employer's block takes its known rows only; the employee's block also prints a component
    # the app does not know (label = its name, no base or rate) but skips the employer's rows;
    # the BVG Age helper row (an age, not money) stays off both.
    rows = []
    for line in lines:
        component = _value(line, "salary_component")
        if component == "BVG Age" or component in skip:
            continue
        known = next((d for d in definitions if d[0] == component), None)
        if known is None and known_only:
            continue
        label = known[1] if known else component
        if component == "Quellensteuer Employee":
            label = _qst_label(employee)
        amount = _num(_value(line, "amount"))
        rows.append({
            "label": label,
            "base": money(_base(slip, assignment, known[3], group) if known else None),
            "rate": pct(_rate(slip, known[2], group) if known else None),
            "amount": money(amount),
            "_amount": amount,
        })
    return rows


def prepare(slip, employee=None, assignment=None, company_name=""):
    """Return the German payslip's header fields, rows and totals from a slip.

    ``slip`` is the Salary Slip (dict or document); ``employee`` and ``assignment`` are separate
    because the slip embeds neither the Employee's fields nor the assignment's insured salary.
    Amounts, bases and rates come back print-ready (money/pct); the numbers behind the rows are
    summed before formatting, so the block totals equal the printed amounts.
    """
    employee = employee or {}
    assignment = assignment or {}
    end_date = _value(slip, "end_date")
    group = bvg_age_group(slip, employee, end_date)
    earnings = _value(slip, "earnings", []) or []
    deduction_lines = _value(slip, "deductions", []) or []
    deduction_rows = _rows(deduction_lines, DEDUCTIONS, slip, employee, assignment, group,
                           skip={d[0] for d in EMPLOYER})
    # the employer shares are Employer Contribution components: the slip keeps them in its employer_contributions
    employer_lines = _value(slip, "employer_contributions", []) or []
    employer_rows = _rows(employer_lines, EMPLOYER, slip, employee, assignment, group, known_only=True)

    gross_ytd = sum(_num(_value(row, "year_to_date")) for row in earnings)
    net_ytd = gross_ytd - sum(
        _num(_value(row, "year_to_date"))
        for row in deduction_lines
        if _value(row, "salary_component") in NET_YTD_COMPONENTS
    ) if gross_ytd else 0.0
    net = _num(_value(slip, "net_pay"))
    rounded = _num(_value(slip, "rounded_total"))
    bank = str(_value(slip, "bank_account_no") or _value(employee, "bank_ac_no") or "")
    digits = "".join(ch for ch in bank if ch.isdigit())
    start, end = _date(_value(slip, "start_date")), _date(end_date)
    period = " – ".join(f"{d:%d.%m.%Y}" for d in (start, end) if d)
    ahv = "".join(ch for ch in str(_value(employee, "social_security_number") or "") if ch.isdigit())
    employment = _value(employee, "employment_type") or ""
    return {
        "company": company_name or _value(slip, "company") or "",
        "employee_name": _value(employee, "employee_name", "") or "",
        "ahv_masked": ("•" * (len(ahv) - 4) + ahv[-4:]) if len(ahv) > 4 else ahv,
        "employment": str(employment),
        "period": period,
        "earnings": [
            {"label": EARNING_LABELS.get(_value(row, "salary_component"), _value(row, "salary_component")),
             "amount": money(_value(row, "amount"))}
            for row in earnings
        ],
        "deductions": [{k: row[k] for k in ("label", "base", "rate", "amount")} for row in deduction_rows],
        "employer_shares": [{k: row[k] for k in ("label", "base", "rate", "amount")} for row in employer_rows],
        "employer_total": money(sum(row["_amount"] for row in employer_rows)),
        "gross": money(_value(slip, "gross_pay")),
        "total_deduction": money(_value(slip, "total_deduction")),
        "net": money(net),
        "paid_amount": money(rounded or net),
        "bank_last_digits": digits[-4:],
        "year_to_date_gross": money(gross_ytd),
        "year_to_date_net": money(net_ytd),
        "qst_canton": _value(employee, "swiss_qst_canton", "") or "",
        "qst_code": _value(employee, "swiss_qst_code", "") or "",
    }

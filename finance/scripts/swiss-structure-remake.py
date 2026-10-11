"""One-shot, idempotent remake of the company's Swiss Monthly salary structure,
run inside a backend container (see swiss-structure-remake.sh).

The live "Swiss Monthly BI Concepts" structure predates the HR 1 decision
(2026-10-11): the six employer shares sit among the deductions and the
employer_contributions table is empty, so an accrual would book none of them.
`bi_payroll.swiss_payroll.ensure_structure` does not replace a structure that
is in use (it logs "Swiss Salary Structure lacks components" instead), so the
manual remake of bi_payroll/README.md ("Salary Structure") runs as this script
(ledgerdemain, 2026-10-11, decision on erp-zg4p):

1. make the new structure with EVERY row of bi_payroll.swiss_rates
   (STRUCTURE_EARNINGS, STRUCTURE_DEDUCTIONS with Quellensteuer Employee last,
   STRUCTURE_EMPLOYER_CONTRIBUTIONS in the employer_contributions table), each
   row carrying its component's formula (structure_rows()), and submit it;
2. per employee with an assignment on the old structure: cancel the submitted one
   and submit the same assignment (same base, same swiss_bvg_insured_salary) on
   the new one; payroll_payable_account is left to HRMS's validate, which fills it
   from the Company's default (set by `swiss-setup.sh payroll_accounts` before
   this script); an employee whose old assignment is already cancelled but who
   has none on the new structure gets it restored, so a rerun after an
   interrupted run completes;
3. cancel the old structure last: HRMS refuses while submitted assignments
   name it (LinkExistsError).

Every step is skipped when its end state already holds, so a rerun changes
nothing (proven on the copy, which was remade for the October parallel proof).

PRIVACY: payroll data is the company's most sensitive data and this repo is
public. The script prints counts, yes/no and document states, never employee
names, salaries or rates. Employee records are referenced by their position in
the run only.
"""
import frappe
from frappe.utils import get_first_day, get_last_day, getdate

COMPANY = "BI Concepts"
OLD_NAME = "Swiss Monthly BI Concepts"

# the assignment fields the remake carries over; payroll_payable_account is
# deliberately not among them: HRMS fills it on validate from the Company's
# default, which is the point of decision (b) on erp-zg4p (2026-10-11).
CARRIED_FIELDS = ["employee", "company", "from_date", "base", "swiss_bvg_insured_salary"]


def say(msg):
    print(msg, flush=True)


def free_name(base):
    """The next free structure name, following the copy's pattern
    (`Swiss Monthly BI Concepts 2` for the second)."""
    taken = {n for n in frappe.get_all("Salary Structure", pluck="name")}
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def find_new_structure():
    """The submitted structure that is not the old one, if any."""
    for name in frappe.get_all(
        "Salary Structure", filters={"docstatus": 1, "company": COMPANY}, pluck="name"
    ):
        if name != OLD_NAME:
            return name
    return None


def create_structure():
    from bi_payroll.swiss_payroll import structure_rows
    from bi_payroll.swiss_rates import (
        STRUCTURE_DEDUCTIONS,
        STRUCTURE_EARNINGS,
        STRUCTURE_EMPLOYER_CONTRIBUTIONS,
    )

    name = free_name(OLD_NAME)
    doc = frappe.get_doc(
        {
            "doctype": "Salary Structure",
            "name": name,
            "company": COMPANY,
            "currency": frappe.get_cached_value("Company", COMPANY, "default_currency"),
            "payroll_frequency": "Monthly",
            "is_active": "Yes",
            "earnings": structure_rows(STRUCTURE_EARNINGS),
            "deductions": structure_rows(STRUCTURE_DEDUCTIONS),
            "employer_contributions": structure_rows(STRUCTURE_EMPLOYER_CONTRIBUTIONS),
        }
    )
    doc.insert()
    doc.submit()
    frappe.db.commit()
    say(
        f"remake: created {name} and submitted it"
        f" (earnings {len(doc.earnings)}, deductions {len(doc.deductions)},"
        f" employer_contributions {len(doc.employer_contributions)})"
    )
    return name


def move_assignments(old_name, new_name):
    """Cancel every submitted assignment on the old structure and submit the
    same one on the new; restore employees left without an active assignment
    by an interrupted earlier run. Counts only, no names."""
    moved = already = restored = 0
    old_rows = frappe.get_all(
        "Salary Structure Assignment",
        filters={"salary_structure": old_name, "docstatus": ["<", 3]},
        fields=["name", "docstatus", "employee"],
    )
    active_on_new = set(
        frappe.get_all(
            "Salary Structure Assignment",
            filters={"salary_structure": new_name, "docstatus": 1},
            pluck="employee",
        )
    )
    for position, row in enumerate(old_rows, start=1):
        if row["employee"] in active_on_new:
            already += 1
            continue
        source = frappe.get_doc("Salary Structure Assignment", row["name"])
        values = {"salary_structure": new_name}
        values.update({field: source.get(field) for field in CARRIED_FIELDS})
        if source.docstatus == 1:
            source.cancel()
            frappe.db.commit()
        doc = frappe.get_doc({"doctype": "Salary Structure Assignment", **values})
        doc.insert()
        doc.submit()
        frappe.db.commit()
        # a second old row of the same employee (an earlier cancelled one) must not make a second assignment
        active_on_new.add(row["employee"])
        if row["docstatus"] == 2:
            restored += 1
            say(f"remake: record {position}: assignment restored on {new_name}")
        else:
            moved += 1
            say(f"remake: record {position}: assignment moved to {new_name}")
    say(f"remake: assignments moved {moved}, restored {restored}, already on the new structure {already}")
    return moved + restored


def cancel_old(old_name):
    """Cancel the old structure once no submitted assignment names it."""
    state = frappe.db.get_value("Salary Structure", old_name, "docstatus")
    if state is None or state == 2:
        return
    blocking = frappe.db.count("Salary Structure Assignment", {"salary_structure": old_name, "docstatus": 1})
    if blocking:
        say(f"remake: {old_name} stays: {blocking} submitted assignment(s) still name it (HRMS refuses)")
        return
    frappe.get_doc("Salary Structure", old_name).cancel()
    frappe.db.commit()
    say(f"remake: {old_name} cancelled")


def payroll_preview():
    """How many employees a Payroll Entry for the running month would pick up
    (HRMS's own employee list, read-only). The read-back of decision (b)."""
    from hrms.payroll.doctype.payroll_entry.payroll_entry import get_employee_list

    today = getdate()
    start = get_first_day(today)
    end = get_last_day(today)
    filters = frappe._dict(
        company=COMPANY,
        currency=frappe.get_cached_value("Company", COMPANY, "default_currency"),
        payroll_frequency="Monthly",
        salary_slip_based_on_timesheet=0,
        # a Payroll Entry carries the Company's default payable, and HRMS's
        # employee list matches the assignments' payroll_payable_account on it:
        # this is why a null payable makes the entry find nobody (decision b)
        payroll_payable_account=frappe.db.get_value("Company", COMPANY, "default_payroll_payable_account"),
        start_date=start,
        end_date=end,
    )
    employees = get_employee_list(filters)
    say(f"remake: payroll preview finds {len(employees)} employee(s) for {start} to {end}")
    return len(employees)


def read_back(new_name):
    """Counts and yes/no only: the mandatory read-backs of decision (b)."""
    doc = frappe.get_doc("Salary Structure", new_name)
    say(
        f"read-back: {new_name} docstatus {doc.docstatus},"
        f" earnings {len(doc.earnings)}, deductions {len(doc.deductions)},"
        f" employer_contributions {len(doc.employer_contributions)}"
    )
    say(f"read-back: employer_contributions rows > 0: {'yes' if len(doc.employer_contributions) > 0 else 'NO'}")
    active = frappe.db.count("Employee", {"status": "Active", "company": COMPANY})
    on_new = frappe.db.count("Salary Structure Assignment", {"salary_structure": new_name, "docstatus": 1})
    say(f"read-back: submitted assignments on the new structure {on_new} of {active} Active employee(s)")
    null_payable = frappe.db.count(
        "Salary Structure Assignment", {"docstatus": 1, "payroll_payable_account": ["is", "not set"]}
    )
    say(f"read-back: submitted assignments with payroll_payable_account null: {null_payable} (must be 0)")
    old_state = frappe.db.get_value("Salary Structure", OLD_NAME, "docstatus")
    say(f"read-back: {OLD_NAME} docstatus {old_state if old_state is not None else 'absent'}")
    payroll_preview()


def main():
    # a live write: runs in the container as the bench user, never as the API user
    frappe.set_user("Administrator")
    new = find_new_structure()
    if new is None:
        new = create_structure()
    else:
        say(f"remake: new structure {new} already exists")
    moved = move_assignments(OLD_NAME, new)
    cancel_old(OLD_NAME)
    if moved:
        say("remake: done")
    read_back(new)


if __name__ == "__main__":
    frappe.init(site="frontend")
    frappe.connect()
    try:
        main()
    finally:
        frappe.destroy()

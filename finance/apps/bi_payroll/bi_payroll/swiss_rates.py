"""The Swiss rate fields and the salary structure's components, as lists: the settings doctype, the copies on the
Salary Slip, the formulas and the structure agree on these names. Pure Python, so the offline tests run without frappe
(test_swiss_payroll.py)."""

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

# Per assignment (Salary Structure Assignment), not from the settings: the insured salary is fixed per year.
ASSIGNMENT_FIELDS = ["swiss_bvg_insured_salary"]

# Order matters: a formula can read only the components above it (BVG Age before the BVG components).
# Commission is paid per month through Additional Salary; the row is there at 0 so the structure names it.
STRUCTURE_EARNINGS = ["Basic Salary", "Commission"]
STRUCTURE_DEDUCTIONS = [
    "BVG Age",
    "AHV/IV/EO Employee",
    "ALV Employee",
    "BVG Employee",
    "NBU Employee",
    "KTG Employee",
    # its amount is set on the slip from the gross, after HRMS's validate (swiss_payroll.withhold_qst)
    "Quellensteuer Employee",
]
# The employer shares are Employer Contribution components: HRMS keeps them in the structure's employer_contributions
# table, off the employee's deductions, and the accrual books each one as an expense against a liability.
STRUCTURE_EMPLOYER_CONTRIBUTIONS = [
    "AHV/IV/EO Employer",
    "ALV Employer",
    "BVG Employer",
    "UVG Employer",
    "KTG Employer",
    "FAK Employer",
]


# The Company's GL accounts of the payroll, by account number (the KMU chart, as bexio booked the payroll in 2020 to 2026).
# The live GL read (erp-nqyb): each payroll Journal Entry debits 5000 and credits 1091 for the net wages, pays 1091 to 1020,
# and credits the employee deductions to 2270, 2271, 2273, 2274 and 2279. ALV and AHV share 2271 in bexio's books.
PAYROLL_PAYABLE_ACCOUNT = (
    "1091"  # Lohndurchlaufkonto: the Company's default payroll payable account
)
# The Earning and Deduction components: one account each (the Salary Component's account row, the liability_account left empty).
# Commission is bexio's 5003 Provisionen: its live journals debit 5003 against 1091, as the Basic Salary's do against 5000.
COMPONENT_ACCOUNTS = {
    "Basic Salary": "5000",  # Löhne
    "Commission": "5003",  # Provisionen
    "AHV/IV/EO Employee": "2271",
    "ALV Employee": "2271",
    "BVG Employee": "2270",
    "NBU Employee": "2273",  # KK Unfallversicherung
    "KTG Employee": "2274",  # KK Krankentaggeld
    "Quellensteuer Employee": "2279",
}
# The employer shares: (expense, liability). The 57xx expense is bexio's (live GL read, erp-nqyb), and the liability is the
# 227x of the same fund: AHV/ALV/FAK are billed together by one Ausgleichskasse, so they share 2271. For UVG bexio credited
# 1183 (an asset: prepaid premiums) on most lines and 2273 on a few; the decision (the bead's note) takes the liability, 2273.
EMPLOYER_CONTRIBUTION_ACCOUNTS = {
    "AHV/IV/EO Employer": ("5700", "2271"),  # AHV, IV, EO, ALV
    "ALV Employer": ("5700", "2271"),
    "BVG Employer": ("5720", "2270"),  # BVG-Beitrag
    "UVG Employer": ("5730", "2273"),  # UVG-Beitrag
    "KTG Employer": ("5740", "2274"),  # KTG-Beitrag
    "FAK Employer": ("5710", "2271"),  # FAK-Beitrag
}


def missing_components(earnings, deductions, employer_contributions):
    """The structure components that a Salary Structure with these component names lacks. ensure_structure makes a
    structure only when the company has none, so one made before a component was added keeps the gap: this names it.
    """
    return (
        [c for c in STRUCTURE_EARNINGS if c not in earnings]
        + [c for c in STRUCTURE_DEDUCTIONS if c not in deductions]
        + [
            c
            for c in STRUCTURE_EMPLOYER_CONTRIBUTIONS
            if c not in employer_contributions
        ]
    )

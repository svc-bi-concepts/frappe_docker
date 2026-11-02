"""Build a private, read-only summary of a Frappe finance site."""

import hashlib
import os
import sys
from decimal import Decimal

DOCTYPES = (
    "GL Entry",
    "Journal Entry",
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Bank Transaction",
    "Salary Slip",
    "Employee",
    "Customer",
    "Supplier",
    "File",
    "Version",
)
API_USER = "api-agent@finance.local"
FILE_BATCH = 500
REMOTE_PREFIXES = ("http://", "https://", "/api/method/")  # Frappe's URL_PREFIXES
SECTIONS = {
    "counts": "count.",
    "trial_balance": "balance.",
    "files": "files.",
    "secrets": "secret.",
}


def _count(db, doctype, suffix=""):
    row = db.sql(f"select count(*) from `tab{doctype}`{suffix}")
    return int(row[0][0] or 0)


def _secret_status(frappe, doctype, name, fieldname):
    """Distinguish an unset secret from one that exists but cannot decrypt."""
    from frappe.utils.password import get_decrypted_password

    exists = frappe.db.sql(
        "select count(*) from `__Auth` where doctype = %s and name = %s "
        "and fieldname = %s and encrypted = 1 and password is not null and password != ''",
        (doctype, name, fieldname),
    )[0][0]
    if not exists:
        return "not set"
    # Frappe's decrypt makes a key and writes it to site_config.json when the site has none.
    if not frappe.local.conf.get("encryption_key"):
        return "fails"
    try:
        value = get_decrypted_password(doctype, name, fieldname, raise_exception=False)
    except Exception:
        return "fails"
    return "ok" if value else "fails"


def _on_disk(roots, url):
    for prefix, root in roots.items():
        if url.startswith(prefix):
            candidate = os.path.realpath(os.path.join(root, url[len(prefix) :]))
            return candidate.startswith(root + os.sep) and os.path.isfile(candidate)
    return False


def _file_counts(frappe):
    # The URL says where a file lies, as in File.get_full_path; is_private only picks the line it counts on.
    roots = {
        "/files/": os.path.realpath(frappe.get_site_path("public", "files")),
        "/private/files/": os.path.realpath(frappe.get_site_path("private", "files")),
    }
    totals = {0: [0, 0], 1: [0, 0]}  # present, missing
    offset = 0
    while True:
        # Folders are File rows without a file; a remote URL has none on this disk either.
        rows = frappe.db.sql(
            "select file_url, file_name, is_private from `tabFile` where is_folder = 0 "
            "order by name limit %s offset %s",
            (FILE_BATCH, offset),
        )
        if not rows:
            break
        for file_url, file_name, is_private in rows:
            private = 1 if is_private else 0
            url = str(file_url or file_name or "")
            if url.startswith(REMOTE_PREFIXES):
                continue
            if "/" not in url:
                url = ("/private/files/" if private else "/files/") + url
            totals[private][0 if _on_disk(roots, url) else 1] += 1
        offset += len(rows)
        if len(rows) < FILE_BATCH:
            break
    return totals


def build_report(frappe):
    lines = []
    for doctype in DOCTYPES:
        lines.append(f"count.{doctype}.total={_count(frappe.db, doctype)}")
        if frappe.get_meta(doctype).is_submittable:
            rows = frappe.db.sql(
                f"select docstatus, count(*) from `tab{doctype}` group by docstatus order by docstatus"
            )
            for docstatus, count in rows:
                lines.append(f"count.{doctype}.docstatus.{int(docstatus)}={int(count)}")

    rows = frappe.db.sql(
        "select account, sum(debit), sum(credit) from `tabGL Entry` "
        "where is_cancelled = 0 group by account order by account"
    )
    for account, debit, credit in rows:
        # Account names are data, so they are written only to the mode-600 report.
        lines.append(
            f"balance.{account}=debit:{Decimal(str(debit or 0)):.2f},credit:{Decimal(str(credit or 0)):.2f}"
        )

    files = _file_counts(frappe)
    for private, label in ((0, "public"), (1, "private")):
        lines.append(f"files.{label}.present={files[private][0]}")
        lines.append(f"files.{label}.missing={files[private][1]}")

    # The helper uses Frappe's canonical decryptor and never returns the value to the report.
    for doctype, name, fieldname, label in (
        ("User", API_USER, "api_secret", "api_user.api_secret"),
        ("Wise Settings", "Wise Settings", "api_token", "wise.api_token"),
        ("PayPal Settings", "PayPal Settings", "client_secret", "paypal.client_secret"),
    ):
        lines.append(
            f"secret.{label}={_secret_status(frappe, doctype, name, fieldname)}"
        )
    return "\n".join(sorted(lines)) + "\n"


def section_digests(report):
    lines = report.splitlines()
    output = []
    for section, prefix in SECTIONS.items():
        canonical = sorted(line for line in lines if line.startswith(prefix))
        digest = hashlib.sha256(
            ("\n".join(canonical) + "\n").encode("utf-8")
        ).hexdigest()
        output.append(f"{section}={digest}")
    return output


def read_digests(path):
    with open(path, encoding="utf-8") as source:
        rows = source.read().splitlines()
    expected = set(SECTIONS)
    parsed = {}
    for row in rows:
        section, separator, digest = row.partition("=")
        if not separator or section not in expected or len(digest) != 64:
            return None
        try:
            int(digest, 16)
        except ValueError:
            return None
        if section in parsed:
            return None
        parsed[section] = digest
    return parsed if set(parsed) == expected else None


def compare_digests(path_a, path_b):
    a = read_digests(path_a)
    b = read_digests(path_b)
    if a is None or b is None:
        print("not equal\ninvalid digest output")
        return 1
    different = [section for section in SECTIONS if a[section] != b[section]]
    if not different:
        print("equal")
        return 0
    print("not equal")
    print("different sections: " + ", ".join(different))
    return 1


def compare_reports(path_a, path_b):
    with open(path_a, encoding="utf-8") as source:
        a = set(source.read().splitlines())
    with open(path_b, encoding="utf-8") as source:
        b = set(source.read().splitlines())
    differences = sorted(a ^ b)
    failed_secrets = sorted(
        line for line in a | b if line.startswith("secret.") and line.endswith("=fails")
    )
    if not differences and not failed_secrets:
        print("equal")
        return 0
    print("not equal")
    for line in differences:
        print(line)
    for line in failed_secrets:
        if line not in differences:
            print(line)
    print(f"differing lines: {len(set(differences) | set(failed_secrets))}")
    return 1


def host_command(argv):
    if argv[1] == "compare":
        return compare_reports(argv[2], argv[3])
    if argv[1] == "digests":
        return compare_digests(argv[2], argv[3])
    with open(argv[2], encoding="utf-8") as source:
        print("\n".join(section_digests(source.read())))
    return 0


def main():
    if len(sys.argv) > 1:
        try:
            raise SystemExit(host_command(sys.argv))
        except OSError as error:
            # 1 means the sites differ; a report that cannot be read says nothing about them.
            print(f"cannot read {error.filename}", file=sys.stderr)
            raise SystemExit(2)
    import frappe

    frappe.init(site="frontend", sites_path=".")
    frappe.connect()
    try:
        sys.stdout.write(build_report(frappe))
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()

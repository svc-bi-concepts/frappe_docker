# erpnextswiss patches

We carry our own fixes to erpnextswiss as patches, applied in the image build. Nothing
goes upstream: the fixes stay with us.

**Pin.** The patches apply to erpnextswiss branch `v16` at commit
`5d85c45b48f4774cda6ba84ab75f138ad45a55c3` (`finance/apps.json`, `finance/apps-copy.json`).
A pin change may stop a patch from applying; the build then fails and names the patch.

## Patches

| Patch | Fixes | Why |
|-------|-------|-----|
| `erpnextswiss/0001-item-tools-syntax.patch` | `erpnextswiss/scripts/item_tools.py` line 43: removes a stray `,` after a SQL string. | At the pin the file does not compile (`SyntaxError: invalid syntax`). Importing it fails, and erpnextswiss's test discovery fails with it. The module holds two `@frappe.whitelist()` functions, `get_next_item_code` and `get_voucher_value`, which cannot be called while it does not compile. Nothing we use imports it (see `swiss.md`, Known issues). |
| `erpnextswiss/0005-expense-claim-grand-total.patch` | The Payment Proposal's expense-claim query takes `grand_total` as the row amount instead of `total_sanctioned_amount`. | A claim's `grand_total` is what is still owed the employee: the sanctioned amount plus the VAT rows (`taxes`) minus the advances. At the pin the run pays the sanctioned amount only, so a claim with a VAT row (the claim route's point, `finance/docs/operating.md`) would be paid short by the VAT and stay Partially Paid. Without taxes and advances, `grand_total` equals the sanctioned amount, so plain claims pay the same. |
| `erpnextswiss/0002-salary-certificate-net-per-form.patch` | `salary_certificate.py`, `fetch_dynamic`: `net_salary` is computed as `gross_salary - deduction_ahv - deduction_pension - deduction_pension_additional` (Ziffer 8 less the total of Ziffer 9 and 10), the way the doctype's own JS 'Summen berechnen' computes it, instead of the slips' `net_pay`. | The ESTV Wegleitung for Form 11 (Ziffer 11): the Nettolohn is the Bruttolohn less the social deductions. The slips' `net_pay` also deducts Quellensteuer (Ziffer 12) and KTG (a Ziffer-15 remark), so the certificate's Nettolohn came out too low (found in erp-hx7s, patched in erp-4yyn). |
| `erpnextswiss/0006-primary-address-format.patch` | `common_functions.py`, `get_primary_address`: the SQL string ends with `.format` — a reference to the method, not a call. The bound method went into `frappe.db.sql` as the query, and the bare `except` swallowed the error: the function always returned `None`. | With the defect, `Payment Proposal.validate` refuses every proposal ("Company address missing"), though the company's address exists and is complete — the payment run and the claim route cannot start at all (found in the erp-j9kn rehearsal; no proposal had ever run against this pin). The fixed query keeps its `%(type)s`/`%(name)s` placeholders, which `frappe.db.sql` substitutes itself. |
| `erpnextswiss/0003-salary-certificate-component-sql.patch` | `salary_certificate.py`, `get_component_dynamic`: the component names go into the query as `IN %s` parameters (with the employee and the dates), not `.format`ed into the SQL string. | The query matches `tabSalary Detail`.`salary_component` (the component's name). A name with an apostrophe broke the SQL at the pin's string formatting, so the certificate raised or summed wrong. |
| `erpnextswiss/0004-salary-certificate-import.patch` | `salary_certificate.py`: `from frappe import _`. | `fetch_values` calls `frappe.msgprint(_("Please select a valid employee."))`; at the pin `_` is not imported, so fetching values with no employee selected raised `NameError`. |
| `erpnextswiss/0008-no-apps-screen-tile.patch` | `erpnextswiss/hooks.py`: removes the `add_to_apps_screen` block (the Schweizer Buchhaltung tile). `app_home`, `app_logo_url` and `after_migrate` stay. | Benchi, 2026-10-12: the apps screen and the app switcher show ERPNext only. The tile was a second entry next to ERPNext, and everything Benchi does daily is on ERPNext's rail (Treasury, Swiss Payroll, HR). The shell's own Workspace Sidebar and its workspaces are left alone: `after_migrate` recreates them on every migrate, and they are unreachable once the tile is gone. |
| `erpnextswiss/0007-form11-fits-a4.patch` | `print_format/605.040.18n_form._11/605.040.18n_form._11.json`, css: the `.form11` rule carries `zoom: 0.69`, so the format's 980 px canvas renders 676 px wide — inside the A4 printable width (210 mm less the 15 mm page margins on both sides = 180 mm = 680 px at 96 dpi; 680/980 = 0.694). | frappe's document PDF path (`download_pdf` → `get_pdf` with its default `smart_shrinking=False`) adds `--disable-smart-shrinking` for wkhtmltopdf > 0.12.3, so the Salary Certificate spilled to two pages, every amount cell empty and the signature block clipped (erp-rqxs). The report path passes `smart_shrinking=True` and was never affected. frappe's shared PDF code stays untouched — every other format on the site is sized for A4 — the form carries the fit instead (erp-gfz9). Proven on the copy: the invented employee's certificate renders as one complete page through `download_pdf`. |

Check a patch by hand against the pin:

```sh
git clone https://github.com/libracore/erpnextswiss && cd erpnextswiss
git checkout 5d85c45
git apply --check ../finance/patches/erpnextswiss/0001-item-tools-syntax.patch
```

The gate (`sh .yardr/check`) runs the apply step over invented trees holding the pinned
sources of the patched files (the `item_tools.py`, `salary_certificate.py`,
`payment_proposal.py` and `common_functions.py` excerpts and the print format JSON's
head in `finance/scripts/test_build_image.py`), so a patch that stops applying to the
pin fails there.

## hrms patches

The one patch to hrms (`finance/patches/hrms/`) is carried the same way: applied at the
image build, checked by the gate, never upstream. hrms is pinned by branch in
`finance/apps.json` (`v16.50.0`), so a bump of hrms re-checks the patch in the gate.

| Patch | Fixes | Why |
|-------|-------|-----|
| `hrms/0001-dock-mounts-on-erpnext.patch` | `hrms/dock/hrms/hrms.json`: adds `"mount_on": "erpnext"` to hrms's shipped dock, and moves its `modified` stamp from `2026-10-05 12:30:00` to `2026-10-12 01:00:00`. | Benchi, 2026-10-12: "add the hr part also in the erp next", one shell, ERPNext. A dock's mount is an app-layer claim: frappe reads it only from the app's own standard dock row, so neither the site nor another app can mount hrms. With the key, the nine HR entries (HR Setup to Tax & Benefits) join ERPNext's rail after Treasury and Swiss Payroll. The stamp is the part a site notices: frappe's migrate imports a shipped document only when the file's `modified` is newer than the stored row's (`frappe/model/sync.py`), so every site that already holds hrms's dock, live included, kept the old row and never got the mount without a forced import. The newer stamp makes a plain `bench migrate` apply it. The apps screen's hiding of hrms is in `frappe/0002`. |

Check it by hand against the pin:

```sh
git clone https://github.com/frappe/hrms && cd hrms
git checkout v16.50.0
git apply --check ../finance/patches/hrms/0001-dock-mounts-on-erpnext.patch
```

The gate runs the apply step over an invented tree holding the pinned `hrms.json`
(`finance/scripts/test_build_image.py`, `test_our_hrms_dock_patch_mounts_the_rail_on_erpnext`,
which also asserts the stamp is newer than the pin's).

## frappe patches

The patches to frappe (`finance/patches/frappe/`) follow the same rules as the hrms ones.

| Patch | Fixes | Why |
|-------|-------|-----|
| `frappe/0002-apps-screen-skips-docked-apps.patch` | `frappe/apps.py`, `get_apps`: an app in `get_docked_apps()` is skipped, right after the `frappe` skip. | frappe's `get_docked_apps` says a mounted app is kept off the apps screen even when it declares `add_to_apps_screen`, but `get_apps` never called it, so hrms's mount hid Frappe HR from the sidebar switcher (`boot.py`, `on_apps_screen`) and left its tile on `/apps`. The one rule covers every mounted app (hrms, and bi_finance and bi_payroll when they carry tiles). An hrms hooks patch that drops `add_to_apps_screen` was the alternative; it would also take hrms's setup-wizard text and route out of frappe's bookkeeping. |

Check it by hand against the pin (frappe is pinned by the image's tag, not by `apps.json`):

```sh
git clone https://github.com/frappe/frappe && cd frappe
git checkout v16.51.0
git apply --check ../finance/patches/frappe/0002-apps-screen-skips-docked-apps.patch
```

The gate applies the frappe directory over invented trees holding the pinned `bundler.py` and the head of
`apps.py` (`test_our_apps_screen_patch_skips_the_docked_apps`).

## How they are applied

The image build fetches the apps with `bench init` (`images/custom/Containerfile`,
builder stage). Right after that, `finance/scripts/apply-patches.sh` applies each
`finance/patches/<app>/*.patch` in file-name order. Name them with a number prefix so the
order is clear. If a patch does not apply, the build stops with
`patch does not apply to apps/erpnextswiss: <patch>`.

The same step runs for the live image and the copy image, since both use the same app
fetch. Patches are kept in the repository, so a rebuild applies the same fixes.

## Adding a patch

1. Make the change in a checkout of the pinned commit, then take the diff with
   `git diff` from the repository root, so the paths read `erpnextswiss/...`.
2. Save it as `finance/patches/erpnextswiss/NNNN-short-name.patch`, with the next number.
3. Add a row to the table above, with what it fixes and why we carry it.
4. Check it applies (above) and that the patched file compiles.
5. Run the gate (`sh .yardr/check`), which tests the apply step.

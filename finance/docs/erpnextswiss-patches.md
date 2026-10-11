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
| `erpnextswiss/0002-salary-certificate-net-per-form.patch` | `salary_certificate.py`, `fetch_dynamic`: `net_salary` is computed as `gross_salary - deduction_ahv - deduction_pension - deduction_pension_additional` (Ziffer 8 less the total of Ziffer 9 and 10), the way the doctype's own JS 'Summen berechnen' computes it, instead of the slips' `net_pay`. | The ESTV Wegleitung for Form 11 (Ziffer 11): the Nettolohn is the Bruttolohn less the social deductions. The slips' `net_pay` also deducts Quellensteuer (Ziffer 12) and KTG (a Ziffer-15 remark), so the certificate's Nettolohn came out too low (found in erp-hx7s, patched in erp-4yyn). |
| `erpnextswiss/0003-salary-certificate-component-sql.patch` | `salary_certificate.py`, `get_component_dynamic`: the component names go into the query as `IN %s` parameters (with the employee and the dates), not `.format`ed into the SQL string. | The query matches `tabSalary Detail`.`salary_component` (the component's name). A name with an apostrophe broke the SQL at the pin's string formatting, so the certificate raised or summed wrong. |
| `erpnextswiss/0004-salary-certificate-import.patch` | `salary_certificate.py`: `from frappe import _`. | `fetch_values` calls `frappe.msgprint(_("Please select a valid employee."))`; at the pin `_` is not imported, so fetching values with no employee selected raised `NameError`. |

Check a patch by hand against the pin:

```sh
git clone https://github.com/libracore/erpnextswiss && cd erpnextswiss
git checkout 5d85c45
git apply --check ../finance/patches/erpnextswiss/0001-item-tools-syntax.patch
```

The gate (`sh .yardr/check`) runs the apply step over invented trees holding the pinned
sources of the patched files (the `item_tools.py` and `salary_certificate.py` excerpts in
`finance/scripts/test_build_image.py`), so a patch that stops applying to the pin fails there.

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

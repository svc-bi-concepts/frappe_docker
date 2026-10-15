// The Quellensteuer code of an Employee is picked from the codes of its canton (QST Tariff Code, filled by the tariff
// loader). The server checks the pair on save (validate_qst_fields); this keeps the form from offering the others.
frappe.ui.form.on("Employee", {
  setup(frm) {
    frm.set_query("swiss_qst_code", () => ({
      query: "bi_payroll.swiss_payroll.search_qst_codes",
      filters: { swiss_qst_canton: frm.doc.swiss_qst_canton },
    }));
  },

  swiss_qst_canton(frm) {
    // a code the new canton does not carry goes; so does the code of a canton that was cleared
    if (!frm.doc.swiss_qst_code) return;
    if (!frm.doc.swiss_qst_canton) {
      frm.set_value("swiss_qst_code", null);
      return;
    }
    frappe.call({
      method: "bi_payroll.swiss_payroll.qst_codes_for_canton",
      args: { canton: frm.doc.swiss_qst_canton },
      callback(r) {
        if (!(r.message || []).includes(frm.doc.swiss_qst_code)) {
          frm.set_value("swiss_qst_code", null);
        }
      },
    });
  },
});

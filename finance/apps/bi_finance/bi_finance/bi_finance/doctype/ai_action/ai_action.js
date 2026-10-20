// Level 2 approval (erp-ss1j): an AI Approver approves or rejects as a separate second step, typing the amount.
// frm.doc.__onload.can_decide is the server's rule (assistant_actions.can_decide); the server checks it again.
frappe.ui.form.on("AI Action", {
  refresh(frm) {
    // the checks table and the dry run text are for reading; every field is read only already
    frm.disable_save();
    if (!(frm.doc.__onload && frm.doc.__onload.can_decide)) {
      return;
    }
    frm.add_custom_button(__("Approve"), () => {
      frappe.prompt(
        {
          fieldname: "confirmed_amount",
          fieldtype: "Data",
          label: __("Type the amount to confirm"),
          reqd: 1,
        },
        (values) => decide(frm, "approve_action", values.confirmed_amount),
        __("Approve {0}", [frm.doc.name]),
        __("Approve"),
      );
    }).addClass("btn-primary");
    frm.add_custom_button(__("Reject"), () => {
      frappe.confirm(__("Reject this action?"), () =>
        decide(frm, "reject_action"),
      );
    });
  },
});

function decide(frm, method, confirmedAmount) {
  frappe.call({
    method: "bi_finance.assistant." + method,
    type: "POST",
    args:
      confirmedAmount === undefined
        ? { action: frm.doc.name }
        : { action: frm.doc.name, confirmed_amount: confirmedAmount },
    freeze: true,
    callback(r) {
      const card = r.message || {};
      // a refusal keeps the row proposed and writes its reason on it, so show it and reload either way
      if (card.ok === false || card.error) {
        frappe.msgprint(card.message || card.error);
      }
      frm.reload_doc();
    },
  });
}

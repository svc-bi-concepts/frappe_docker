// The VAT Declaration's Desk buttons for the payment basis (vereinnahmte Entgelte), see vat_declaration.py.
// erpnextswiss's own Get values reads the invoice basis, so the form says so in the intro instead of hiding it.
// Every write has a preview first: the dialog shows the figures of a dry run, and only its button writes the draft.

frappe.ui.form.on('VAT Declaration', {
    refresh: function(frm) {
        if (frm.doc.docstatus === 0) {
            frm.set_intro(__("Payment basis (vereinnahmte Entgelte): use Fill from payments, not Get values"), "yellow");
        }
        if (!frm.is_new()) {
            var button = frm.add_custom_button(__("Fill from payments"), function() {
                fill_from_payments(frm);
            });
            // a submitted declaration is never changed by the fill
            if (frm.doc.docstatus === 1) {
                button.prop("disabled", true);
            }
        }
    }
});

// The period the fill takes ('2026Q3' or '2026H1', as the method reads it), from the form's start and end dates.
// Returns null when the dates are not one quarter or half-year.
function period_of(frm) {
    var start = frm.doc.start_date || "";
    var end = frm.doc.end_date || "";
    if (start.slice(0, 4) !== end.slice(0, 4)) {
        return null;
    }
    var year = start.slice(0, 4);
    var first = parseInt(start.slice(5, 7), 10);
    var last = parseInt(end.slice(5, 7), 10);
    var lastDay = new Date(parseInt(year, 10), last, 0).getDate();
    if (start.slice(8) !== "01" || parseInt(end.slice(8), 10) !== lastDay) {
        return null;
    }
    if (last - first === 2 && (first - 1) % 3 === 0) {
        return year + "Q" + ((first - 1) / 3 + 1);
    }
    if (last - first === 5 && (first === 1 || first === 7)) {
        return year + (first === 1 ? "H1" : "H2");
    }
    return null;
}

function fill_from_payments(frm) {
    var period = period_of(frm);
    if (!period) {
        frappe.msgprint(__("Start and End must be one quarter (Q1 to Q4) or one half-year, in the same year."));
        return;
    }
    frappe.call({
        method: "bi_finance.vat_declaration.fill_payment_basis",
        args: { company: frm.doc.company, period: period, dry_run: 1 },
        freeze: true,
        callback: function(r) {
            if (r.message) {
                show_preview(frm, period, r.message.fields);
            }
        }
    });
}

// The figures of the dry run, the amounts (Float fields, with their Ziffer in the label) and not the rates.
function show_preview(frm, period, fields) {
    var rows = "";
    Object.keys(fields).forEach(function(name) {
        var df = frappe.meta.get_docfield("VAT Declaration", name);
        if (!df || df.fieldtype !== "Float") {
            return;
        }
        rows += "<tr><td>" + frappe.utils.escape_html(df.label) + "</td><td style='text-align: right'>"
            + format_amount(fields[name]) + "</td></tr>";
    });
    var html = "<p>" + __("Nothing is written until you confirm. The draft is not submitted.") + "</p>"
        + "<table class='table table-bordered'><thead><tr><th>" + __("Field") + "</th><th style='text-align: right'>"
        + __("CHF") + "</th></tr></thead><tbody>" + rows + "</tbody></table>";

    var dialog = new frappe.ui.Dialog({
        title: __("Fill from payments: {0}", [period]),
        fields: [{ fieldtype: "HTML", fieldname: "preview", options: html }],
        primary_action_label: __("Write draft"),
        primary_action: function() {
            frappe.call({
                method: "bi_finance.vat_declaration.fill_payment_basis",
                args: { company: frm.doc.company, period: period, dry_run: 0 },
                freeze: true,
                callback: function() {
                    dialog.hide();
                    frm.reload_doc();
                }
            });
        }
    });
    dialog.show();
}

// the dry run sends the amounts as strings with their decimals
function format_amount(value) {
    return parseFloat(value).toFixed(2);
}

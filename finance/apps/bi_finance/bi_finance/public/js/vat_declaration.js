// The VAT Declaration's Desk buttons for the payment basis (vereinnahmte Entgelte), see vat_declaration.py.
// erpnextswiss's own Get values reads the invoice basis, so the form says so in the intro instead of hiding it.
// Every write has a preview first: the dialog shows the figures of a dry run, and only its button writes the draft.

frappe.ui.form.on("VAT Declaration", {
  refresh: function (frm) {
    if (frm.doc.docstatus === 0) {
      frm.set_intro(
        __(
          "Payment basis (vereinnahmte Entgelte): Fill from payments fills this draft; Recalculate updates 289/299.",
        ),
        "yellow",
      );
      // erpnextswiss adds Get values before this runs (doctype_js loads after the app's own): it reads the invoice basis
      frm.remove_custom_button(__("Get values"));
    }
    // onload sets the dates and then erpnextswiss's default title, so the title is set here, after both
    if (frm.is_new()) {
      set_title(frm);
    }
    if (
      !frm.is_new() ||
      (frm.doc.company && frm.doc.start_date && frm.doc.end_date)
    ) {
      var button = frm.add_custom_button(__("Fill from payments"), function () {
        fill_from_payments(frm);
      });
      // a submitted declaration is never changed by the fill
      if (frm.doc.docstatus === 1) {
        button.prop("disabled", true);
      }
    }
  },
  start_date: function (frm) {
    set_title(frm);
  },
  end_date: function (frm) {
    set_title(frm);
  },
});

// The title of a new declaration is its period, the name the fill gives the draft (MWST 2026Q3, vat_declaration.py).
// erpnextswiss's default 'Qn / YYYY - abbr' is replaced, and so is our own title when the dates change; any other title is Benchi's.
var DEFAULT_TITLE = /^Q[1-4] \/ \d{4}( - .*)?$/;
var PERIOD_TITLE = /^MWST \d{4}(Q[1-4]|H[12])$/;

function set_title(frm) {
  var period = period_of(frm);
  if (!frm.is_new() || !period) {
    return;
  }
  var title = frm.doc.title || "";
  if (title !== "" && !DEFAULT_TITLE.test(title) && !PERIOD_TITLE.test(title)) {
    return;
  }
  if (title !== "MWST " + period) {
    frm.set_value("title", "MWST " + period);
  }
}

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
    frappe.msgprint(
      __(
        "Start and End must be one quarter (Q1 to Q4) or one half-year, in the same year.",
      ),
    );
    return;
  }
  frappe.call({
    method: "bi_finance.vat_declaration.fill_payment_basis",
    args: { company: frm.doc.company, period: period, dry_run: 1 },
    freeze: true,
    callback: function (r) {
      if (r.message) {
        show_preview(frm, period, r.message.fields);
      }
    },
  });
}

// The figures of the dry run, the amounts (Float fields, with their Ziffer in the label) and not the rates.
function show_preview(frm, period, fields) {
  var rows = "";
  Object.keys(fields).forEach(function (name) {
    var df = frappe.meta.get_docfield("VAT Declaration", name);
    if (!df || df.fieldtype !== "Float") {
      return;
    }
    rows +=
      "<tr><td>" +
      frappe.utils.escape_html(df.label) +
      "</td><td style='text-align: right'>" +
      format_amount(fields[name]) +
      "</td></tr>";
  });
  var html =
    "<p>" +
    __("Nothing is written until you confirm. The draft is not submitted.") +
    "</p>" +
    "<table class='table table-bordered'><thead><tr><th>" +
    __("Field") +
    "</th><th style='text-align: right'>" +
    __("CHF") +
    "</th></tr></thead><tbody>" +
    rows +
    "</tbody></table>";

  var dialog = new frappe.ui.Dialog({
    title: __("Fill from payments: {0}", [period]),
    fields: [{ fieldtype: "HTML", fieldname: "preview", options: html }],
    primary_action_label: __("Write draft"),
    primary_action: function () {
      frappe.call({
        method: "bi_finance.vat_declaration.fill_payment_basis",
        args: { company: frm.doc.company, period: period, dry_run: 0 },
        freeze: true,
        callback: function (r) {
          dialog.hide();
          // the fill creates or updates the period's draft; the unsaved new form is left, not saved
          frappe.set_route("Form", "VAT Declaration", r.message.name);
        },
      });
    },
  });
  dialog.show();
}

// the dry run sends the amounts as strings with their decimals
function format_amount(value) {
  return parseFloat(value).toFixed(2);
}

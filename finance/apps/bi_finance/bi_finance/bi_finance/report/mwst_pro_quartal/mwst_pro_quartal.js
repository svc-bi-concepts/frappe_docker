frappe.query_reports["MWST pro Quartal"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "year",
			label: __("Year"),
			fieldtype: "Int",
			default: new Date().getFullYear(),
			reqd: 1,
		},
		{
			// empty: the four quarters of the year as columns; a quarter: it alone
			fieldname: "quarter",
			label: __("Quarter"),
			fieldtype: "Select",
			options: "\nQ1\nQ2\nQ3\nQ4",
		},
		{
			// 'Belege' lists one Ziffer's vouchers; the Ziffern view's row click sets it
			fieldname: "ansicht",
			label: __("View"),
			fieldtype: "Select",
			options: "Ziffern\nBelege",
			default: "Ziffern",
		},
		{
			fieldname: "ziffer",
			label: __("Ziffer"),
			fieldtype: "Data",
			depends_on: "eval:doc.ansicht=='Belege'",
		},
	],
	onload(report) {
		// A click on a Ziffern row opens its vouchers: the same report in the Belege view, that Ziffer and
		// the quarter of the clicked column (the first quarter shown, from the label). One bound handler on the
		// wrapper survives the re-renders.
		// (the datatable does not exist yet at onload, so the handler sits on the page's wrapper)
		$(report.page.wrapper).on("click", ".mwst-ziffer", function () {
			frappe.query_report.set_filter_value("ziffer", this.dataset.ziffer);
			frappe.query_report.set_filter_value("quarter", this.dataset.quarter);
			frappe.query_report.set_filter_value("ansicht", "Belege");
		});
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		// The label cell and every quarter cell carry the click target: the Ziffer and the filter's quarter (Q1..Q4),
		// the label's the first quarter shown, a quarter cell's its own (the column's fieldname starts '2026Q3 ').
		if (data && (frappe.query_report.get_filter_value("ansicht") || "Ziffern") === "Ziffern") {
			const match = /^\d{4}(Q[1-4]) /.exec(column.fieldname || "");
			let quarter = match ? match[1] : "";
			if (column.fieldname === "label") {
				const first = Object.keys(data).find((key) => / erpnext$/.test(key));
				quarter = first ? first.slice(4, 6) : "";
			}
			if (quarter) {
				value = `<span class="mwst-ziffer" data-ziffer="${frappe.utils.escape_html(data.ziffer)}" data-quarter="${quarter}" style="cursor: pointer; text-decoration: underline dotted">${value}</span>`;
			}
		}
		// a Belege row's voucher opens the document
		if (data && column.fieldname === "name" && data.doctype) {
			const route = `${frappe.router.slug(data.doctype)}/${encodeURIComponent(data.name)}`;
			value = `<a href="/app/${route}">${frappe.utils.escape_html(data.name)}</a>`;
		}
		return value;
	},
};

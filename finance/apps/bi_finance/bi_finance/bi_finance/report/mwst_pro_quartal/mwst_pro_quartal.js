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
		// its quarter (or the year's first quarter, when the whole year is shown). One bound handler on the
		// wrapper survives the re-renders.
		report.datatable.$wrapper.on("click", ".mwst-ziffer", function () {
			frappe.query_report.set_filter_value("ziffer", this.dataset.ziffer);
			frappe.query_report.set_filter_value("quarter", this.dataset.quarter);
			frappe.query_report.set_filter_value("ansicht", "Belege");
		});
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		// the Ziffer's cell carries the click target; the quarter rides along for the Belege view
		if (data && column.fieldname === "label" && (frappe.query_report.get_filter_value("ansicht") || "Ziffern") === "Ziffern") {
			const quarters = ["Q1", "Q2", "Q3", "Q4"]
				.map((q) => `${frappe.query_report.get_filter_value("year") || new Date().getFullYear()}${q}`)
				.filter((q) => data[`${q} erpnext`] !== undefined);
			const quarter = quarters.length === 1 ? quarters[0] : (quarters[0] || "");
			value = `<span class="mwst-ziffer" data-ziffer="${frappe.utils.escape_html(data.ziffer)}" data-quarter="${quarter}" style="cursor: pointer; text-decoration: underline dotted">${value}</span>`;
		}
		// a Belege row's voucher opens the document
		if (data && column.fieldname === "name" && data.doctype) {
			const route = `${frappe.router.slug(data.doctype)}/${encodeURIComponent(data.name)}`;
			value = `<a href="/app/${route}">${frappe.utils.escape_html(data.name)}</a>`;
		}
		return value;
	},
};

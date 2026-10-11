frappe.query_reports["Payroll Hand-over"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "year",
			label: __("Year"),
			fieldtype: "Int",
			reqd: 1,
			default: frappe.datetime.get_today().substr(0, 4),
		},
		{
			fieldname: "month",
			label: __("Month"),
			fieldtype: "Select",
			options: "\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12",
			description: __("Empty: one row per employee for the whole year"),
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: "Hand-over\nLohnausweis\nQuellensteuer Abrechnung\nJahresdeklarationen",
			default: "Hand-over",
			description: __(
				"Lohnausweis: the amounts by the Form 11 lines. Quellensteuer Abrechnung: the withholding per canton. " +
				"Jahresdeklarationen: the year's wage sums per insurance (the month is not used)"
			),
		},
		{
			fieldname: "period",
			label: __("Period (Quellensteuer)"),
			fieldtype: "Select",
			options: "Month\nQuarter",
			default: "Month",
			description: __("Quellensteuer Abrechnung: one row per canton and month, or per quarter"),
		},
		{
			fieldname: "francs",
			label: __("Whole francs"),
			fieldtype: "Check",
			default: 0,
			description: __(
				"Lohnausweis only: round the lines to whole francs, round half up, for the printed certificate or the export. The view keeps rappen."
			),
		},
	],
};

import frappe
from bi_finance import assistant_actions
from frappe.model.document import Document


class AIAction(Document):
    def onload(self):
        # the form's Approve / Reject buttons read this, so the rule lives in one place (assistant_actions.can_decide)
        self.set_onload(
            "can_decide", assistant_actions.can_decide(self, frappe.session.user)
        )

"""The Bank Transaction Rules for the recurring lines that have no invoice (bank fees, interest). Pure data, no site.

patches/bank_transaction_rules.py makes them once per company, as Bank Transaction Rule records that Benchi edits in the
Desk; the accounts are looked up there by number, so no account name is written here. A rule takes a line when its
transaction type fits and one of its texts is in the description ('Contains', as ERPNext's evaluate_rule reads it).

The texts are the feed's own wording: a payout's fee row reads 'Wise fee: <reference>' (wise_client.feed_rows); the UBS
lines carry the bank's words (Spesen, Gebühr, Kontoführung, Preis für, Zins, Interest).
"""

RULES = (
    {"rule_name": "Wise fees", "transaction_type": "Withdrawal", "account_number": "6940",
     "texts": ("Wise fee", "Wise Charges")},
    {"rule_name": "UBS bank fees", "transaction_type": "Withdrawal", "account_number": "6940",
     "texts": ("Spesen", "Gebühr", "Kontoführung", "Preis für")},
    {"rule_name": "UBS interest received", "transaction_type": "Deposit", "account_number": "6950",
     "texts": ("Zins", "Interest")},
    {"rule_name": "UBS interest paid", "transaction_type": "Withdrawal", "account_number": "6900",
     "texts": ("Zins", "Interest")},
)

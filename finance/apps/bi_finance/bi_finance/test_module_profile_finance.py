"""The Module Profile "Finance" patch: the block list, the upsert, which users get it, and that a
rerun is silent.

Stdlib only, with a stub frappe that keeps the Module Profile and User rows in memory and copies
the profile rows onto a user on save, the way User.validate_allowed_modules does. The names are
invented or public product module names; no company data.

    python3 -m unittest bi_finance.test_module_profile_finance
"""

import contextlib
import io
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))

# the modules the bead keeps (the public product names), asserted absent from the block list
KEPT = [
    "Accounts",
    "BI Finance",
    "BI Payroll",
    "Buying",
    "Contacts",
    "Core",
    "Custom",
    "Desk",
    "Email",
    "ERPNextSwiss",
    "Expenses",
    "Geo",
    "HR",
    "HR Setup",
    "Payroll",
    "Printing",
    "Selling",
    "Setup",
    "System",
    "Tax and Benefits",
    "Users",
    "Workflow",
]

USERS = {
    "ada@example.com": {"enabled": 1, "user_type": "System User", "module_profile": None},
    "grace@example.com": {
        "enabled": 1,
        "user_type": "System User",
        "module_profile": "Own",
    },
    "alan@example.com": {"enabled": 0, "user_type": "System User", "module_profile": None},
    "web@example.com": {"enabled": 1, "user_type": "Website User", "module_profile": None},
}


class Row(dict):
    """A child-table row, readable by key and by attribute the way a frappe document is."""

    def __getattr__(self, key):
        return self[key]


class Doc:
    def __init__(self, doctype, fields):
        self.doctype = doctype
        fields = dict(fields)
        # a child table passed by its field name becomes the rows the property serves
        rows = fields.pop("block_modules", None)
        if rows is not None:
            fields["_block_modules"] = [Row(r) for r in rows]
        self.__dict__.update(fields)
        self.saved = False

    @property
    def block_modules(self):
        return getattr(self, "_block_modules", [])

    def append(self, key, row):
        rows = getattr(self, f"_{key}", None)
        if rows is None:
            rows = self.__dict__[f"_{key}"] = []
        rows.append(Row(row))

    def set(self, key, rows):
        self.__dict__[f"_{key}"] = list(rows)

    def insert(self, ignore_permissions=True):
        self.saved = True

    def save(self, ignore_permissions=True):
        self.saved = True
        if self.doctype == "User":
            # the document persists back onto the row, as frappe's save would
            row = self.frappe.users[self.name]
            row.update({k: v for k, v in self.__dict__.items() if k in row})
            if self.module_profile:
                # what User.validate_allowed_modules does
                self.set(
                    "block_modules",
                    list(self.frappe.profiles[self.module_profile].block_modules),
                )
                # the copied rows persist, as the child table would
                row["block_modules"] = self._block_modules


class Db:
    """Namespace for db.exists, the one db call the patch makes."""

    def __init__(self, store):
        self.store = store

    def exists(self, doctype, name):
        return name if name in self.store.profiles else None


class Frappe(types.ModuleType):
    """The two doctypes as rows; get_all matches the filters, db.exists looks the profile up."""

    def __init__(self, profile=None):
        super().__init__("frappe")
        self.profiles = {} if profile is None else {profile.pop("module_profile_name"): profile}
        self.users = {k: dict(v) for k, v in USERS.items()}
        self.cache_clears = 0
        self.db = Db(self)

    def get_doc(self, arg, name=None):
        if isinstance(arg, dict):
            doc = Doc(arg["doctype"], arg)
            doc.frappe = self
            if arg["doctype"] == "Module Profile":
                self.profiles[arg["module_profile_name"]] = doc
            return doc
        if arg == "Module Profile":
            doc = self.profiles[name]
            doc.frappe = self
            return doc
        user = Doc("User", {**self.users[name], "name": name})
        user.frappe = self
        return user

    def get_all(self, doctype, filters=None, pluck=None):
        assert doctype == "User"
        found = [
            name
            for name, row in self.users.items()
            if all(
                (row.get(k) == v) or (v == ("is", "not set") and not row.get(k))
                for k, v in (filters or {}).items()
            )
        ]
        return sorted(found) if pluck else [{"name": n} for n in sorted(found)]

    def clear_cache(self):
        self.cache_clears += 1


with mock.patch.dict(sys.modules, {"frappe": Frappe()}):
    from bi_finance.patches import module_profile_finance as patch


class Base(unittest.TestCase):
    def setUp(self):
        self.frappe = Frappe()
        self.addCleanup(setattr, patch, "frappe", patch.frappe)
        patch.frappe = self.frappe

    def run_patch(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            patch.execute()
        return out.getvalue()


class TheList(Base, unittest.TestCase):
    def test_the_block_list_holds_distinct_public_module_names(self):
        self.assertTrue(patch.BLOCKED_MODULES)
        self.assertEqual(len(patch.BLOCKED_MODULES), len(set(patch.BLOCKED_MODULES)))
        self.assertEqual(patch.BLOCKED_MODULES, sorted(patch.BLOCKED_MODULES, key=str.lower))
        for m in patch.BLOCKED_MODULES:
            self.assertIsInstance(m, str)

    def test_the_kept_modules_are_not_blocked(self):
        for m in KEPT:
            self.assertNotIn(m, patch.BLOCKED_MODULES, m)


class Upsert(Base, unittest.TestCase):
    def test_a_missing_profile_is_created_with_the_blocked_rows_in_order(self):
        out = self.run_patch()
        profile = self.frappe.profiles["Finance"]
        self.assertEqual(profile.saved, True)
        self.assertEqual([r.module for r in profile.block_modules], patch.BLOCKED_MODULES)
        self.assertEqual(out, "module_profile_finance: profile created, 1 users assigned\n")

    def test_a_profile_whose_rows_match_is_left_alone(self):
        self.frappe.profiles["Finance"] = Doc(
            "Module Profile",
            {
                "module_profile_name": "Finance",
                "_block_modules": [Row(module=m) for m in patch.BLOCKED_MODULES],
            },
        )
        out = self.run_patch()
        self.assertEqual(self.frappe.profiles["Finance"].saved, False)
        self.assertEqual(out, "module_profile_finance: profile unchanged, 1 users assigned\n")

    def test_a_profile_whose_rows_differ_is_updated(self):
        self.frappe.profiles["Finance"] = Doc(
            "Module Profile",
            {"module_profile_name": "Finance", "_block_modules": [Row(module="Accounts")]},
        )
        out = self.run_patch()
        profile = self.frappe.profiles["Finance"]
        self.assertEqual(profile.saved, True)
        self.assertEqual([r.module for r in profile.block_modules], patch.BLOCKED_MODULES)
        self.assertIn("profile updated", out)


class Users(Base, unittest.TestCase):
    def test_only_enabled_system_users_without_a_profile_are_assigned(self):
        self.run_patch()
        ada = self.frappe.users["ada@example.com"]
        self.assertEqual(ada["module_profile"], "Finance")
        doc = self.frappe.get_doc("User", "ada@example.com")
        self.assertEqual([r.module for r in doc.block_modules], patch.BLOCKED_MODULES)
        # a user with a profile of their own, a disabled user, a website user: untouched
        self.assertEqual(self.frappe.users["grace@example.com"]["module_profile"], "Own")
        self.assertIsNone(self.frappe.users["alan@example.com"]["module_profile"])
        self.assertIsNone(self.frappe.users["web@example.com"]["module_profile"])

    def test_a_rerun_is_silent(self):
        self.run_patch()
        out = self.run_patch()
        self.assertEqual(out, "")


class Cache(Base, unittest.TestCase):
    def test_the_cache_is_cleared_when_something_changed_and_not_on_a_rerun(self):
        self.run_patch()
        self.assertEqual(self.frappe.cache_clears, 1)
        self.run_patch()
        self.assertEqual(self.frappe.cache_clears, 1)


class Listed(unittest.TestCase):
    def test_the_patch_is_listed_after_the_model_sync(self):
        with open(os.path.join(HERE, "patches.txt"), encoding="utf-8") as f:
            post = f.read().split("[post_model_sync]")[1]
        self.assertIn("\nbi_finance.patches.module_profile_finance\n", post)


if __name__ == "__main__":
    unittest.main()

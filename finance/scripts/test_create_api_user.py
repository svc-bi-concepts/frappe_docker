"""Offline tests for create-api-user.sh: the three users, the exact role lists and the refusal.

docker is a stub that runs the script's inline container Python against a fake frappe (a JSON
file for the site's users), so the role logic really runs. HOME is a temp folder, the keys are
whatever the fake generates. No site, no network.
Run with: cd finance/scripts && python3 -m unittest test_create_api_user
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "create-api-user.sh")

# The stub finds the "-" that ends `python -I -`, reads the container Python from stdin and
# runs it with a fake frappe in sys.modules. Users live in $FAKE_SITE as {email: {...}}.
DOCKER_STUB = """#!/usr/bin/env python3
import json, os, sys, types

argv = sys.argv[1:]
with open(os.environ["DOCKER_LOG"], "a") as f:
    f.write(" ".join(argv) + "\\n")
if os.environ.get("DOCKER_FAIL"):
    sys.exit(1)
rest = argv[argv.index("-I") + 2:]
site_path = os.environ["FAKE_SITE"]
site = json.load(open(site_path)) if os.path.exists(site_path) else {}


class Row:
    def __init__(self, role):
        self.role = role


class User:
    def __init__(self, email, data):
        self.email = email
        self.api_key = data.get("api_key")
        self.roles = [Row(r) for r in data.get("roles", [])]

    def append(self, table, row):
        self.roles.append(Row(row["role"]))

    def set(self, table, rows):
        self.roles = list(rows)

    def _store(self):
        site[self.email] = {"roles": [r.role for r in self.roles], "api_key": self.api_key}

    def insert(self, ignore_permissions=False):
        self._store()

    def save(self, ignore_permissions=False):
        self._store()


frappe = types.ModuleType("frappe")
frappe.init = lambda **kw: None
frappe.connect = lambda: None
frappe.set_user = lambda user: None
frappe.db = types.SimpleNamespace(
    exists=lambda doctype, email: email in site,
    commit=lambda: json.dump(site, open(site_path, "w")),
)


def get_doc(doctype, email=None):
    if isinstance(doctype, dict):
        return User(doctype["email"], {"roles": [r["role"] for r in doctype["roles"]]})
    return User(email, site[email])


def generate_keys(email):
    site[email]["api_key"] = "generated-key-" + email.split("@")[0]
    return {"api_key": site[email]["api_key"], "api_secret": "generated-secret-" + email.split("@")[0]}


frappe.get_doc = get_doc
for name in ("frappe.core", "frappe.core.doctype", "frappe.core.doctype.user"):
    sys.modules[name] = types.ModuleType(name)
users = types.ModuleType("frappe.core.doctype.user.user")
users.generate_keys = generate_keys
sys.modules["frappe"] = frappe
sys.modules["frappe.core.doctype.user.user"] = users

sys.argv = ["-"] + rest
exec(compile(sys.stdin.read(), "container.py", "exec"))
"""

AGENT = "api-agent@finance.local"
SYNC = "api-sync@finance.local"
MCP = "api-mcp@finance.local"
MCP_ROLES = {"Accounts User", "Sales User", "Purchase User", "Stock User"}
AGENT_ROLES = MCP_ROLES | {
    "Sales Master Manager",
    "Purchase Master Manager",
    "Item Manager",
    "HR User",
}
SYNC_ROLES = AGENT_ROLES - {"HR User"}


class CreateApiUser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.yard = os.path.join(self.home, "ws_yardr_finance")
        os.makedirs(self.yard)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as f:
            f.write(DOCKER_STUB)
        os.chmod(stub, 0o755)
        self.site = os.path.join(self.tmp.name, "site.json")
        self.docker_log = os.path.join(self.tmp.name, "docker.log")
        self.script = self.install(SCRIPT)

    def install(self, source):
        """A copy of the script two folders below a temp root, as the script expects (it cds up from itself)."""
        folder = os.path.join(self.tmp.name, "repo", "finance", "scripts")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "create-api-user.sh")
        shutil.copy(source, target)
        return target

    def run_script(self, *args, **extra):
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "HOME": self.home,
            "FAKE_SITE": self.site,
            "DOCKER_LOG": self.docker_log,
        }
        env.update(extra)
        return subprocess.run(
            ["sh", self.script, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def users(self):
        with open(self.site) as f:
            return json.load(f)

    def keys_file(self, name):
        return os.path.join(self.yard, name)

    def seed(self, email, roles, api_key="old-key"):
        with open(self.site, "w") as f:
            json.dump({email: {"roles": roles, "api_key": api_key}}, f)

    def assertNoSecrets(self, proc):
        for text in (proc.stdout, proc.stderr):
            self.assertNotIn("generated-key", text)
            self.assertNotIn("generated-secret", text)

    # ---- the three users -----------------------------------------------------------

    def test_default_is_the_agent_with_its_keys_in_the_old_place(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(self.users()[AGENT]["roles"]), AGENT_ROLES)
        path = self.keys_file(".erpnext-api")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNoSecrets(proc)

    def test_sync_gets_the_agent_list_without_hr_user_and_its_own_key_file(self):
        proc = self.run_script("--user", "sync")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(self.users()[SYNC]["roles"]), SYNC_ROLES)
        self.assertNotIn("HR User", self.users()[SYNC]["roles"])
        path = self.keys_file(".erpnext-api-sync")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.keys_file(".erpnext-api")))
        self.assertNoSecrets(proc)

    def test_mcp_gets_four_roles_and_its_own_key_file(self):
        proc = self.run_script("--user", "mcp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(self.users()[MCP]["roles"]), MCP_ROLES)
        path = self.keys_file(".erpnext-api-mcp")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            text = f.read()
        self.assertIn("api_key=generated-key-api-mcp", text)
        self.assertNoSecrets(proc)

    def test_the_role_list_is_printed_after_the_run(self):
        proc = self.run_script("--user", "mcp")
        self.assertIn(
            "roles now: Accounts User, Purchase User, Sales User, Stock User",
            proc.stdout,
        )

    def test_a_second_run_changes_nothing_and_keeps_the_keys(self):
        self.run_script("--user", "mcp")
        with open(self.keys_file(".erpnext-api-mcp")) as f:
            before = f.read()
        proc = self.run_script("--user", "mcp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("keys unchanged", proc.stdout)
        self.assertNotIn("roles set", proc.stdout)
        self.assertNotIn("roles removed", proc.stdout)
        with open(self.keys_file(".erpnext-api-mcp")) as f:
            self.assertEqual(f.read(), before)

    def test_rotate_writes_new_keys_for_the_chosen_user_only(self):
        self.run_script("--user", "mcp")
        self.seed(MCP, sorted(MCP_ROLES), api_key="old-key")
        proc = self.run_script("--rotate", "--user", "mcp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("wrote", proc.stdout)
        self.assertFalse(os.path.exists(self.keys_file(".erpnext-api")))

    def test_a_missing_keys_file_is_made_again(self):
        self.run_script("--user", "sync")
        os.remove(self.keys_file(".erpnext-api-sync"))
        proc = self.run_script("--user", "sync")
        self.assertIn("wrote", proc.stdout)
        self.assertTrue(os.path.exists(self.keys_file(".erpnext-api-sync")))

    # ---- the lists are exact for sync and mcp, additive for the agent ----------------

    def test_mcp_loses_every_role_outside_its_list(self):
        self.seed(MCP, ["Accounts User", "HR User", "System Manager", "Desk User"])
        proc = self.run_script("--user", "mcp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(self.users()[MCP]["roles"]), MCP_ROLES | {"Desk User"})
        self.assertIn("roles removed: HR User, System Manager", proc.stdout)
        self.assertNotIn(
            "Desk User", proc.stdout.split("roles removed:")[1].splitlines()[0]
        )

    def test_sync_loses_hr_user(self):
        self.seed(SYNC, sorted(AGENT_ROLES))
        proc = self.run_script("--user", "sync")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(set(self.users()[SYNC]["roles"]), SYNC_ROLES)
        self.assertIn("roles removed: HR User", proc.stdout)

    def test_the_agent_only_adds_roles_and_keeps_system_manager(self):
        self.seed(AGENT, ["Accounts User", "System Manager"])
        proc = self.run_script("--user", "agent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            set(self.users()[AGENT]["roles"]), AGENT_ROLES | {"System Manager"}
        )
        self.assertNotIn("roles removed", proc.stdout)

    # ---- the refusals --------------------------------------------------------------

    def widened(self, list_name, role):
        """The script with `role` added to one of its lists, as a later edit would do."""
        with open(SCRIPT) as f:
            text = f.read()
        marker = f'{list_name}_ROLES="'
        self.assertIn(marker, text)
        edited = os.path.join(self.tmp.name, "edited.sh")
        with open(edited, "w") as f:
            f.write(text.replace(marker, marker + role + "|", 1))
        self.script = self.install(edited)

    def test_a_widened_list_is_refused_before_docker(self):
        for list_name in ("SYNC", "MCP"):
            for role in ("System Manager", "HR User", "HR Manager", "AI Approver"):
                with self.subTest(list_name=list_name, role=role):
                    self.widened(list_name, role)
                    proc = self.run_script("--user", "agent")
                    self.assertEqual(proc.returncode, 2, proc.stdout)
                    self.assertIn("refusing to run", proc.stderr)
                    self.assertIn(role, proc.stderr)
                    self.assertFalse(os.path.exists(self.docker_log))
                    self.assertFalse(os.path.exists(self.site))

    def test_an_unknown_user_or_argument_is_a_usage_error(self):
        for args in (["--user", "root"], ["--user"], ["--bogus"]):
            with self.subTest(args=args):
                proc = self.run_script(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("usage", proc.stderr)
        self.assertFalse(os.path.exists(self.docker_log))

    def test_a_failing_container_leaves_no_keys_file(self):
        proc = self.run_script("--user", "mcp", DOCKER_FAIL="1")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(self.keys_file(".erpnext-api-mcp")))
        self.assertFalse(os.path.exists(self.keys_file(".erpnext-api-mcp.tmp")))


if __name__ == "__main__":
    unittest.main()

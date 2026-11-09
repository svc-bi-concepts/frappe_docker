"""Dev restore contract: compose profile gating, mounts, and the restore script's refusals, on invented data.

The script runs on the host with stub MariaDB tools and a stub age that decrypts nothing. Real decryption and a real
MariaDB were checked by hand against the same script in the mariadb:11.8 and app images (finance/docs/hetzner-dev-handoff.md,
"First start"); these tests pin the logic around them. No Docker daemon is needed: compose only renders the file.
"""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "finance/deploy/finance-coolify.dev.yml"
SCRIPT = ROOT / "finance/scripts/dev_restore.sh"
IMAGE = "registry.example.test/finance@sha256:" + "a" * 64
PREFIX = "20260101_000000-frontend"
DB_NAME = "_fx1234abcd"
DB_PASSWORD = "Fx7kQ2pL9mRz0aB3"
MEMBERS = {
    f"{PREFIX}-database.sql.gz": None,  # filled in below: gzip of an invented table
    f"{PREFIX}-site_config_backup.json": json.dumps(
        {"db_name": DB_NAME, "db_password": DB_PASSWORD, "encryption_key": "invented"},
        indent=1,
    ).encode(),
    f"{PREFIX}-files.tar": None,
    f"{PREFIX}-private-files.tar": None,
}
ENV = {
    "FINANCE_DEV_IMAGE": IMAGE,
    "FINANCE_MUTE_EMAILS": "1",
    "FINANCE_PAUSE_SCHEDULER": "1",
    "FINANCE_DISABLE_EXTERNAL_INTEGRATIONS": "1",
    "DB_ROOT_PASSWORD": "invented-root",
}
RESTORE = ("restore-db", "restore-files")

STUB_AGE = """#!/bin/sh
# Decrypts nothing: the synthetic bundle is plain tar. Fails when the test asks for a decrypt failure.
[ "${AGE_STUB_FAIL:-0}" = 1 ] && exit 1
for last; do :; done
cat "$last"
"""

# Each stub records its arguments and stdin, so a test can see what the script would have sent.
STUB_RECORD = """#!/bin/sh
echo "$(basename "$0") $*" >>"$STUB_LOG"
cat >>"$STUB_LOG.stdin" 2>/dev/null
exit 0
"""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def members():
    import gzip

    dump = gzip.compress(b"CREATE TABLE fx (id INT);\nINSERT INTO fx VALUES (42);\n")
    public = tar_bytes({"sites/frontend/public/files/invented.txt": b"invented\n"})
    private = tar_bytes({"sites/frontend/private/files/invented.txt": b"invented\n"})
    return {
        f"{PREFIX}-database.sql.gz": dump,
        f"{PREFIX}-site_config_backup.json": MEMBERS[
            f"{PREFIX}-site_config_backup.json"
        ],
        f"{PREFIX}-files.tar": public,
        f"{PREFIX}-private-files.tar": private,
    }


class ComposeTests(unittest.TestCase):
    def render(self, profiles=()):
        args = ["docker", "compose", "-f", str(COMPOSE)]
        for profile in profiles:
            args += ["--profile", profile]
        return subprocess.run(
            [*args, "config", "--format", "json"],
            cwd=ROOT,
            env={
                **{k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"},
                **ENV,
                "COMPOSE_ENV_FILES": "",
                "COMPOSE_DISABLE_ENV_FILE": "1",
            },
            capture_output=True,
            text=True,
        )

    def test_restore_services_absent_without_profile(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in RESTORE:
            with self.subTest(service=name):
                self.assertNotIn(name, services)

    def test_restore_services_shape(self):
        result = self.render(("restore",))
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in RESTORE:
            service = services[name]
            with self.subTest(service=name):
                self.assertEqual(service["network_mode"], "none")
                self.assertEqual(service["restart"], "no")
                self.assertEqual(service["labels"], {"traefik.enable": "false"})
                self.assertFalse(service.get("ports"))
                self.assertFalse(service.get("networks"))
                self.assertEqual(service["profiles"], ["restore"])
                self.assertEqual(service["entrypoint"][-1] in ("db", "files"), True)
                for volume in service["volumes"]:
                    if volume["source"].startswith("finance-dev-"):
                        self.assertTrue(volume.get("read_only"), volume)
        self.assertEqual(services["restore-files"]["image"], IMAGE)
        self.assertEqual(services["restore-db"]["image"], "mariadb:11.8")
        self.assertEqual(
            services["restore-files"]["depends_on"]["restore-db"]["condition"],
            "service_completed_successfully",
        )

    def test_restore_writes_only_its_targets(self):
        services = json.loads(self.render(("restore",)).stdout)["services"]
        db_targets = {v["target"] for v in services["restore-db"]["volumes"]}
        files_targets = {v["target"] for v in services["restore-files"]["volumes"]}
        self.assertEqual(
            db_targets, {"/var/lib/mysql", "/restore/staging", "/restore/identity"}
        )
        self.assertEqual(
            files_targets,
            {
                "/home/frappe/frappe-bench/sites",
                "/restore/staging",
                "/restore/identity",
            },
        )

    def test_dev_init_and_its_dependents_unchanged(self):
        services = json.loads(self.render(("restore",)).stdout)["services"]
        self.assertEqual(services["dev-init"]["network_mode"], "none")
        self.assertFalse(services["dev-init"].get("depends_on"))
        for name, service in services.items():
            if name in RESTORE or name == "dev-init":
                continue
            with self.subTest(service=name):
                self.assertNotIn(
                    "restore-db", json.dumps(service.get("depends_on", {}))
                )
                self.assertNotIn(
                    "restore-files", json.dumps(service.get("depends_on", {}))
                )

    def test_labels_are_list_form_in_source(self):
        # Coolify's raw parser appends numeric label entries, which compose refuses in a mapping but takes as list
        # items (note 3782). Compose renders both forms the same, so the source file is read here.
        lines = COMPOSE.read_text().splitlines()
        blocks = [i for i, line in enumerate(lines) if line.strip() == "labels:"]
        self.assertFalse(
            [line for line in lines if line.strip().startswith("traefik.enable:")]
        )
        for i in blocks:
            with self.subTest(line=i + 1):
                self.assertEqual(lines[i + 1].strip(), "- traefik.enable=false")
        services = json.loads(self.render(("restore", "workers")).stdout)["services"]
        self.assertEqual(len(blocks), len(services))
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(service["labels"], {"traefik.enable": "false"})

    def test_restore_inputs_are_external(self):
        volumes = json.loads(self.render(("restore",)).stdout)["volumes"]
        for name in ("finance-dev-restore-staging", "finance-dev-age-identity"):
            with self.subTest(volume=name):
                self.assertTrue(volumes[name].get("external"), volumes[name])

    def test_db_root_password_required_for_restore(self):
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE),
                "--profile",
                "restore",
                "config",
                "-q",
            ],
            cwd=ROOT,
            env={
                **{k: v for k, v in os.environ.items() if k != "DB_ROOT_PASSWORD"},
                **{k: v for k, v in ENV.items() if k != "DB_ROOT_PASSWORD"},
                "COMPOSE_ENV_FILES": "",
                "COMPOSE_DISABLE_ENV_FILE": "1",
            },
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dev-restore-test-"))
        self.staging = self.tmp / "staging"
        (self.staging / "tools").mkdir(parents=True)
        self.identity = self.tmp / "identity" / "identity.txt"
        self.identity.parent.mkdir()
        self.identity.write_text("invented identity\n")
        self.datadir = self.tmp / "datadir"
        self.datadir.mkdir()
        self.sites = self.tmp / "sites"
        self.sites.mkdir()
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        self.log = self.tmp / "stub.log"

        age = self.staging / "tools" / "age"
        age.write_text(STUB_AGE)
        age.chmod(0o755)
        for name in ("mariadb", "mariadb-install-db", "mariadbd", "mariadb-admin"):
            stub = self.stubs / name
            stub.write_text(STUB_RECORD)
            stub.chmod(0o755)
        self.write_bundle(members())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_bundle(self, contents, manifest_override=None):
        lines = []
        for name, data in contents.items():
            lines.append(f"{sha256(data)}  {name}")
        manifest = (
            manifest_override
            if manifest_override is not None
            else "\n".join(lines) + "\n"
        )
        bundle = tar_bytes({"SHA256SUMS": manifest.encode(), **contents})
        (self.staging / "finance-dev-backup.tar.age").write_bytes(bundle)
        digest = sha256(bundle)
        (self.staging / "finance-dev-backup.tar.age.sha256").write_text(
            f"{digest}  finance-dev-backup.tar.age\n"
        )
        self.pin = sha256((self.staging / "tools" / "age").read_bytes())

    def run_script(self, mode, env_extra=None, use_pin=True):
        environment = {
            "PATH": f"{self.stubs}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.tmp),
            "STUB_LOG": str(self.log),
            "DB_ROOT_PASSWORD": "invented-root",
            "DEV_RESTORE_STAGING": str(self.staging),
            "DEV_RESTORE_IDENTITY": str(self.identity),
            "DEV_RESTORE_DATADIR": str(self.datadir),
            "DEV_RESTORE_SITES": str(self.sites),
        }
        if use_pin:
            environment["DEV_RESTORE_AGE_SHA256"] = self.pin
        environment.update(env_extra or {})
        return subprocess.run(
            ["bash", str(SCRIPT), mode],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def stub_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def site_files(self):
        return sorted(
            str(p.relative_to(self.sites)) for p in self.sites.rglob("*") if p.is_file()
        )

    def test_db_success_streams_dump_and_creates_user(self):
        result = self.run_script("db")
        self.assertEqual(result.returncode, 0, result.stderr)
        stdin_log = Path(str(self.log) + ".stdin").read_text()
        self.assertIn(f"CREATE DATABASE {DB_NAME}", stdin_log)
        self.assertIn(
            f"CREATE USER '{DB_NAME}'@'%' IDENTIFIED BY '{DB_PASSWORD}'", stdin_log
        )
        self.assertIn("CREATE TABLE fx", stdin_log)
        self.assertIn("ALTER USER 'root'@'localhost'", stdin_log)
        self.assertIn("mariadbd --user=mysql", self.stub_calls())

    def test_files_success_writes_site_and_attachments(self):
        result = self.run_script("files")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.site_files(),
            [
                "frontend/private/files/invented.txt",
                "frontend/public/files/invented.txt",
                "frontend/site_config.json",
            ],
        )
        config = self.sites / "frontend" / "site_config.json"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(config.read_text())["db_name"], DB_NAME)

    def test_files_refuses_existing_site_then_overwrites_on_request(self):
        (self.sites / "frontend").mkdir()
        (self.sites / "frontend" / "site_config.json").write_text("{}")
        result = self.run_script("files")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already holds a site", result.stderr)
        self.assertEqual(
            (self.sites / "frontend" / "site_config.json").read_text(), "{}"
        )
        result = self.run_script("files", {"FINANCE_DEV_RESTORE_OVERWRITE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("frontend/site_config.json", self.site_files())

    def test_db_refuses_non_empty_datadir_and_leaves_it_alone(self):
        (self.datadir / "existing").write_text("keep")
        result = self.run_script("db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not empty", result.stderr)
        self.assertEqual((self.datadir / "existing").read_text(), "keep")
        self.assertEqual(self.stub_calls(), "")

    def test_db_overwrite_replaces_datadir(self):
        (self.datadir / "existing").write_text("replaced")
        result = self.run_script("db", {"FINANCE_DEV_RESTORE_OVERWRITE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.datadir / "existing").exists())

    def test_overwrite_must_be_zero_or_one(self):
        result = self.run_script("db", {"FINANCE_DEV_RESTORE_OVERWRITE": "yes"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be 0 or 1", result.stderr)

    def test_bad_ciphertext_checksum_fails_before_any_write(self):
        bundle = self.staging / "finance-dev-backup.tar.age"
        bundle.write_bytes(bundle.read_bytes()[:-1] + b"x")
        for mode in ("db", "files"):
            with self.subTest(mode=mode):
                result = self.run_script(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ciphertext does not match", result.stderr)
                self.assertEqual(self.stub_calls(), "")
                self.assertEqual(self.site_files(), [])
                self.assertEqual(list(self.datadir.iterdir()), [])

    def test_member_checksum_mismatch_fails_before_any_write(self):
        contents = members()
        manifest = "\n".join(
            (
                f"{'c' * 64}  {name}"
                if name.endswith("-files.tar")
                else f"{sha256(data)}  {name}"
            )
            for name, data in contents.items()
        )
        self.write_bundle(contents, manifest_override=manifest + "\n")
        result = self.run_script("files")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match its checksum", result.stderr)
        self.assertEqual(self.site_files(), [])

    def test_missing_member_fails(self):
        contents = members()
        ghost = f"{PREFIX}-private-files.tar"
        manifest = "\n".join(
            (
                f"{'d' * 64}  {PREFIX}-ghost-private-files.tar"
                if name == ghost
                else f"{sha256(data)}  {name}"
            )
            for name, data in contents.items()
        )
        contents.pop(ghost)
        self.write_bundle(contents, manifest_override=manifest + "\n")
        result = self.run_script("db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing from the archive", result.stderr)
        self.assertEqual(list(self.datadir.iterdir()), [])

    def test_decrypt_failure_is_reported(self):
        result = self.run_script("db", {"AGE_STUB_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot decrypt the archive", result.stderr)
        self.assertEqual(self.stub_calls(), "")

    def test_unpinned_age_binary_is_refused(self):
        result = self.run_script("db", use_pin=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the pinned", result.stderr)

    def test_compact_site_config_is_read_too(self):
        contents = members()
        contents[f"{PREFIX}-site_config_backup.json"] = json.dumps(
            {"db_name": DB_NAME, "db_password": DB_PASSWORD}
        ).encode()
        self.write_bundle(contents)
        result = self.run_script("db")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"CREATE USER '{DB_NAME}'@'%' IDENTIFIED BY '{DB_PASSWORD}'",
            Path(str(self.log) + ".stdin").read_text(),
        )

    def test_password_needing_an_escape_is_refused_before_any_write(self):
        contents = members()
        contents[f"{PREFIX}-site_config_backup.json"] = json.dumps(
            {"db_name": DB_NAME, "db_password": "has\\backslash"}
        ).encode()
        self.write_bundle(contents)
        result = self.run_script("db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no plain db_password", result.stderr)
        self.assertEqual(list(self.datadir.iterdir()), [])
        self.assertEqual(self.stub_calls(), "")


if __name__ == "__main__":
    unittest.main()

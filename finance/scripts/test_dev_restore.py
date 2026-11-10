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
ROOT_PASSWORD = "invented-root"
# What the restore gives the dev site: a fixed name and a password derived from the dev root password.
DB_NAME = "finance_dev"
DB_PASSWORD = hashlib.sha256(
    b"finance-dev site database user\n" + ROOT_PASSWORD.encode()
).hexdigest()
# The source site's configuration, all invented. None of its values may reach dev.
SOURCE_CONFIG = {
    "db_name": "_fx1234abcd",
    "db_password": "Fx7kQ2pL9mRz0aB3",
    "encryption_key": "Zx9InventedSourceKey0000000000000000000000=",
    "host_name": "https://source.example.test",
    "mail_password": "invented-mail-secret",
}
ENV = {
    "FINANCE_DEV_IMAGE": IMAGE,
    "FINANCE_MUTE_EMAILS": "1",
    "FINANCE_PAUSE_SCHEDULER": "1",
    "FINANCE_DISABLE_EXTERNAL_INTEGRATIONS": "1",
    "DB_ROOT_PASSWORD": ROOT_PASSWORD,
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
        f"{PREFIX}-site_config_backup.json": json.dumps(
            SOURCE_CONFIG, indent=1
        ).encode(),
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
            "DB_ROOT_PASSWORD": ROOT_PASSWORD,
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
            # The stubs read their stdin to the end: an open terminal or pipe of the caller would hold them forever.
            stdin=subprocess.DEVNULL,
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

    def test_db_sets_up_the_accounts_a_fresh_db_volume_has(self):
        # The db service's entrypoint skips its init on a volume that holds a database, so without these its
        # healthcheck.sh fails on every restored volume (probed in mariadb:11.8).
        result = self.run_script("db")
        self.assertEqual(result.returncode, 0, result.stderr)
        stdin_log = Path(str(self.log) + ".stdin").read_text()
        self.assertIn(f"CREATE USER 'root'@'%' IDENTIFIED BY '{ROOT_PASSWORD}'", stdin_log)
        self.assertIn("GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION", stdin_log)
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertIn(f"CREATE USER 'healthcheck'@'{host}'", stdin_log)
        cnf = self.datadir / ".my-healthcheck.cnf"
        self.assertEqual(cnf.stat().st_mode & 0o777, 0o600)
        lines = cnf.read_text().splitlines()
        self.assertEqual(lines[0], "[mariadb-client]")
        self.assertIn("user=healthcheck", lines)
        password = next(line for line in lines if line.startswith("password="))
        self.assertIn(f"IDENTIFIED BY '{password[len('password='):]}'", stdin_log)
        self.assertNotIn(password, result.stdout + result.stderr)

    def test_failed_extraction_leaves_no_site_config(self):
        # dev-init starts the resource once site_config.json exists: a half-restored site must not have one.
        contents = members()
        contents[f"{PREFIX}-private-files.tar"] = b"not a tar archive\n"
        self.write_bundle(contents)
        result = self.run_script("files")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot extract the private files", result.stderr)
        self.assertNotIn("frontend/site_config.json", self.site_files())

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
        self.assertEqual(
            json.loads(config.read_text()),
            {
                "db_name": DB_NAME,
                "db_password": DB_PASSWORD,
                "db_type": "mariadb",
                "disable_scheduler": 1,
                "host_name": "http://frontend:8080",
            },
        )

    def test_nothing_of_the_source_site_config_reaches_dev(self):
        # The source encryption_key would make the passwords stored in the restored database readable on dev, and
        # the source database credentials are not dev's (handoff doc, section 3).
        for mode in ("db", "files"):
            result = self.run_script(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            written = (
                (self.sites / "frontend" / "site_config.json").read_text()
                if mode == "files"
                else ""
            )
            sent = Path(str(self.log) + ".stdin").read_text() + self.stub_calls()
            for key, value in SOURCE_CONFIG.items():
                with self.subTest(mode=mode, key=key):
                    self.assertNotIn(value, written)
                    self.assertNotIn(value, sent)
                    self.assertNotIn(value, result.stdout + result.stderr)
        self.assertNotIn(
            "encryption_key",
            json.loads((self.sites / "frontend" / "site_config.json").read_text()),
        )

    def test_both_services_agree_on_the_site_database_password(self):
        self.assertEqual(self.run_script("db").returncode, 0)
        self.assertEqual(self.run_script("files").returncode, 0)
        config = json.loads((self.sites / "frontend" / "site_config.json").read_text())
        self.assertIn(
            f"CREATE USER '{config['db_name']}'@'%' IDENTIFIED BY '{config['db_password']}'",
            Path(str(self.log) + ".stdin").read_text(),
        )
        # Dev's password follows dev's root password, never a value of the backup.
        other = self.run_script(
            "files",
            {
                "DB_ROOT_PASSWORD": "invented-other",
                "FINANCE_DEV_RESTORE_OVERWRITE": "1",
            },
        )
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertNotEqual(
            json.loads((self.sites / "frontend" / "site_config.json").read_text())[
                "db_password"
            ],
            config["db_password"],
        )

    def test_root_password_is_required_before_any_write(self):
        for mode in ("db", "files"):
            with self.subTest(mode=mode):
                result = self.run_script(mode, {"DB_ROOT_PASSWORD": ""})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DB_ROOT_PASSWORD is not set", result.stderr)
                self.assertEqual(self.stub_calls(), "")
                self.assertEqual(self.site_files(), [])

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


if __name__ == "__main__":
    unittest.main()

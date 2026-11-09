"""Dev startup contract, using invented configuration and no Docker daemon."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "finance/deploy/finance-coolify.dev.yml"
IMAGE = "registry.example.test/finance@sha256:" + "a" * 64
REQUIRED_SWITCHES = {
    "FINANCE_MUTE_EMAILS": "1",
    "FINANCE_PAUSE_SCHEDULER": "1",
    "FINANCE_DISABLE_EXTERNAL_INTEGRATIONS": "1",
}
EXPECTED_SITE_FLAGS = {
    "mute_emails": 1,
    "pause_scheduler": 1,
    "disable_external_integrations": 1,
}
ENV = {
    **REQUIRED_SWITCHES,
    "FINANCE_DEV_IMAGE": IMAGE,
    "DB_ROOT_PASSWORD": "invented-root",
}


class InitTests(unittest.TestCase):
    def run_init(self, path, environment):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from pathlib import Path; "
                "from dev_site_init import initialize; initialize(Path(sys.argv[1]), os.environ)",
                str(path),
            ],
            cwd=ROOT / "finance/scripts",
            env={
                **{k: v for k, v in os.environ.items() if k not in ENV},
                **environment,
            },
            capture_output=True,
            text=True,
        )

    def test_switches_fail_without_partial_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site_config.json"
            original = '{"db_name": "invented_dev", "mute_emails": 0}'
            for name in REQUIRED_SWITCHES:
                for value in (None, "", "0", "true", " 1", "2"):
                    with self.subTest(name=name, value=value):
                        environment = dict(ENV)
                        if value is None:
                            del environment[name]
                        else:
                            environment[name] = value
                        path.write_text(original)
                        self.assertNotEqual(
                            self.run_init(path, environment).returncode, 0
                        )
                        self.assertEqual(path.read_text(), original)
                        path.unlink()
                        self.assertNotEqual(
                            self.run_init(path, environment).returncode, 0
                        )
                        self.assertFalse(path.exists())

    def test_image_refused_before_writing(self):
        for image in ("", "finance:main", "finance@sha256:abc", IMAGE + ":tag"):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "site_config.json"
                path.write_text("{}")
                self.assertNotEqual(
                    self.run_init(path, {**ENV, "FINANCE_DEV_IMAGE": image}).returncode,
                    0,
                )
                self.assertEqual(path.read_text(), "{}")

    def test_success_preserves_credentials_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site_config.json"
            path.write_text('{"db_name": "invented_dev", "pause_scheduler": 0}')
            for _ in range(2):
                result = self.run_init(path, ENV)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(path.read_text()),
                    {
                        "db_name": "invented_dev",
                        **EXPECTED_SITE_FLAGS,
                    },
                )
                self.assertEqual(list(path.parent.iterdir()), [path])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_malformed_site_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site_config.json"
            self.assertNotEqual(self.run_init(path, ENV).returncode, 0)
            self.assertFalse(path.exists())
            for contents in ("broken JSON", "[]", "null"):
                path.write_text(contents)
                self.assertNotEqual(self.run_init(path, ENV).returncode, 0)
                self.assertEqual(path.read_text(), contents)


class ComposeTests(unittest.TestCase):
    def render(self, environment):
        return subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
            cwd=ROOT,
            env={
                **os.environ,
                **environment,
                "COMPOSE_ENV_FILES": "",
                "COMPOSE_DISABLE_ENV_FILE": "1",
            },
            capture_output=True,
            text=True,
        )

    def test_isolated_startup_graph(self):
        result = self.render(ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertTrue(config["networks"])
        self.assertTrue(
            all(
                n.get("internal") is True and not n.get("external")
                for n in config["networks"].values()
            )
        )
        services = config["services"]
        self.assertNotIn("create-site", services)
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(service["labels"], {"traefik.enable": "false"})
                self.assertFalse(service.get("ports"))
                self.assertNotEqual(service.get("restart"), "unless-stopped")
                if name == "dev-init":
                    self.assertEqual(service["network_mode"], "none")
                    self.assertFalse(service.get("depends_on"))
                else:
                    self.assertEqual(
                        service["depends_on"]["dev-init"]["condition"],
                        "service_completed_successfully",
                    )
                    self.assertEqual(set(service["networks"]), {"ledger-dev"})
                    self.assertFalse(service.get("network_mode"))
                if name not in ("db", "redis-cache", "redis-queue"):
                    self.assertEqual(service["image"], IMAGE)
                    self.assertNotEqual(service.get("pull_policy"), "never")
        init = services["dev-init"]
        self.assertEqual(init["environment"]["FINANCE_DEV_IMAGE"], IMAGE)
        for key, value in REQUIRED_SWITCHES.items():
            self.assertEqual(init["environment"][key], value)
        self.assertEqual(
            Path(config["configs"]["dev-site-init"]["file"]),
            ROOT / "finance/scripts/dev_site_init.py",
        )
        self.assertIn("/dev-site-init.py", init["entrypoint"])
        self.assertEqual(init["configs"][0]["target"], "/dev-site-init.py")
        for name in ("backend", "queue-long", "queue-short", "scheduler", "websocket"):
            self.assertEqual(
                services[name]["depends_on"]["configurator"]["condition"],
                "service_completed_successfully",
            )

    def test_required_env(self):
        for key in ("FINANCE_DEV_IMAGE", *REQUIRED_SWITCHES):
            with self.subTest(key=key):
                self.assertNotEqual(self.render({**ENV, key: ""}).returncode, 0)


if __name__ == "__main__":
    unittest.main()

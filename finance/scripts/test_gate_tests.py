"""Image gate contract, exercised in a disposable bench without Docker or Frappe."""

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "finance/scripts"


class ImageGate(unittest.TestCase):
    def run_gate(self, failing=False, empty=False):
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp) / "bench"
            scripts = bench / "finance/scripts"
            scripts.mkdir(parents=True)
            shutil.copy(SCRIPTS / "gate-tests.sh", scripts)
            # Real unittest discovery/imports with synthetic cases prove failure propagation.
            (scripts / "gate-modules.sh").write_text(
                "set --\n" if empty else "set -- test_first test_second\n"
            )
            app = bench / "apps/bi_finance"
            app.mkdir(parents=True)
            for name in ("first", "second"):
                (app / f"test_{name}.py").write_text(
                    "import unittest\nclass Case(unittest.TestCase):\n"
                    f"    def test_case(self): self.assertTrue({not failing or name == 'first'})\n"
                )
            python = bench / "env/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            return subprocess.run(
                ["sh", str(scripts / "gate-tests.sh")],
                cwd=tmp,
                env={"PATH": os.defpath, "HOME": tmp, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
            )

    def test_success_runs_both_modules_and_summarizes_on_stdout(self):
        run = self.run_gate()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Ran 2 tests", run.stderr)
        self.assertIn("gate-tests: PASS (2 modules)", run.stdout)

    def test_failure_is_nonzero_and_summarized_on_stdout(self):
        run = self.run_gate(failing=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("Ran 2 tests", run.stderr)
        self.assertIn("failures=1", run.stderr)
        self.assertIn("gate-tests: FAIL (2 modules, exit 1)", run.stdout)

    def test_empty_manifest_refuses_a_false_green(self):
        run = self.run_gate(empty=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn("empty module list", run.stderr)

    def test_manifest_is_sourceable_by_posix_shell(self):
        run = subprocess.run(
            [
                "sh",
                "-c",
                '. "$1"; printf "%s\\n" "$@"',
                "sh",
                str(SCRIPTS / "gate-modules.sh"),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        modules = run.stdout.splitlines()
        self.assertEqual(len(modules), 13)
        self.assertEqual(len(set(modules)), 13)
        self.assertTrue(all(m.startswith("bi_finance.test_") for m in modules))


def build_stages(source):
    lines = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    stages = {}
    current = None
    for line in lines.replace("\\\n", " ").splitlines():
        if not line.strip():
            continue
        op, _, value = line.strip().partition(" ")
        if op == "FROM":
            base, _, current = shlex.split(value)
            stages[current] = {"base": base, "instructions": []}
        elif current:
            stages[current]["instructions"].append((op, value))
    return stages


class FinanceBuildGraph(unittest.TestCase):
    """Static Dockerfile graph proof; the first real consumer build runs in Coolify."""

    def test_finance_target_inherits_backend_without_serializing_secrets(self):
        stages = build_stages((ROOT / "images/custom/Containerfile").read_text())
        self.assertEqual(stages["builder"]["base"], "base")
        self.assertEqual(stages["backend"]["base"], "base")
        self.assertEqual(stages["finance"]["base"], "backend")
        self.assertEqual(stages["default"]["base"], "backend")
        for stage in stages.values():
            for op, value in stage["instructions"]:
                if op == "ARG":
                    self.assertNotEqual(value.split("=", 1)[0], "APPS_JSON")
                elif op == "ENV":
                    self.assertNotIn(
                        "APPS_JSON",
                        dict(
                            word.split("=", 1)
                            for word in shlex.split(value)
                            if "=" in word
                        ),
                    )


class FinanceBuildExecution(unittest.TestCase):
    """Run build shell instructions with bench, pip and patch tools at fake boundaries.

    The oracle is the live-image contract: pins before patches before assets,
    no Swiss desk records, and both editable apps plus the executable gate.
    Distro provisioning is outside this daemon-free fixture's scope.
    """

    def test_build_executes_patches_and_installs_image_contents(self):
        self.run_build({"APPS_JSON": "fixture-apps"}, "fixture-apps")

    def test_missing_secret_uses_repository_apps(self):
        self.run_build({}, (ROOT / "finance/apps.json").read_text())

    def test_empty_secrets_use_repository_apps(self):
        self.run_build(
            {"APPS_JSON": "", "apps_json": ""},
            (ROOT / "finance/apps.json").read_text(),
        )

    def test_primary_secret_overrides_legacy_and_repository(self):
        self.run_build(
            {"APPS_JSON": "primary-apps", "apps_json": "legacy-apps"}, "primary-apps"
        )

    def test_empty_primary_secret_uses_legacy(self):
        self.run_build({"APPS_JSON": "", "apps_json": "legacy-apps"}, "legacy-apps")

    def test_legacy_secret_overrides_repository(self):
        self.run_build({"apps_json": "legacy-apps"}, "legacy-apps")

    def run_build(self, secrets, expected_apps):
        stages = build_stages((ROOT / "images/custom/Containerfile").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            tools = root / "bin"
            tools.mkdir()
            calls = root / "calls"
            stub = tools / "boundary"
            stub.write_text(f"#!{sys.executable}\n" + r"""
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
bench = pathlib.Path(os.environ['BENCH'])
log = pathlib.Path(os.environ['CALLS'])
event = [name]
if name == 'bench' and args[0] == 'init':
    assert '--skip-assets' in args, args
    secret = next(a.split('=', 1)[1] for a in args if a.startswith('--apps_path='))
    assert pathlib.Path(secret).read_text() == os.environ['EXPECTED_APPS']
    for path in ('env/bin', 'sites/assets', 'apps/frappe', 'apps/hrms',
                 'apps/erpnextswiss/erpnextswiss/erpnextswiss/workspace',
                 'apps/erpnextswiss/erpnextswiss/desktop_icon',
                 'apps/erpnextswiss/erpnextswiss/workspace_sidebar'):
        (bench / path).mkdir(parents=True, exist_ok=True)
    (bench / 'env/bin/pip').symlink_to(pathlib.Path(sys.argv[0]).resolve())
    event += ['init']
elif name == 'bench' and args == ['build', '--production']:
    event += ['build', sorted(str(p.relative_to(bench)) for p in bench.rglob('*') if p.is_dir())]
elif name == 'python3':
    assert pathlib.Path(args[0]).is_file(), args
    assert pathlib.Path(args[1]).read_text() == os.environ['EXPECTED_APPS']
    assert pathlib.Path(args[2]).is_dir(), args
    event += ['pins']
elif name == 'sh':
    assert pathlib.Path(args[0]).is_file(), args
    assert list(pathlib.Path(args[1]).glob('*.patch')), args
    assert pathlib.Path(args[1]).name == pathlib.Path(args[2]).name, args
    assert pathlib.Path(args[2]).is_dir(), args
    event += [pathlib.Path(args[2]).name]
elif name == 'pip':
    assert args[:3] == ['install', '--no-cache-dir', '-e'], args
    app = pathlib.Path(args[3])
    assert (app / 'pyproject.toml').is_file(), args
    event += [app.name]
else:
    raise AssertionError((name, args))
with log.open('a') as f:
    f.write(json.dumps(event) + '\n')
""")
            stub.chmod(0o755)
            for name in ("bench", "python3", "sh"):
                (tools / name).symlink_to(stub)
            outputs = {}
            for name in ("builder", "backend", "finance"):
                stage_root = root / name
                parent = stages[name]["base"]
                if parent in outputs:
                    shutil.copytree(outputs[parent], stage_root, symlinks=True)
                else:
                    stage_root.mkdir()
                outputs[name] = stage_root
                bench = stage_root / "home/frappe/frappe-bench"
                env = dict(
                    os.environ,
                    PATH=str(tools) + os.pathsep + os.defpath,
                    BENCH=str(bench),
                    CALLS=str(calls),
                    EXPECTED_APPS=expected_apps,
                    FRAPPE_BRANCH="fixture",
                    FRAPPE_PATH="fixture",
                    CACHE_BUST="",
                )
                cwd = stage_root
                for op, value in stages[name]["instructions"]:
                    if op == "COPY":
                        words = shlex.split(value)
                        options = {}
                        while words[0].startswith("--"):
                            key, val = words.pop(0)[2:].split("=", 1)
                            options[key] = val
                        origin = outputs[options["from"]] if "from" in options else ROOT
                        dest = stage_root / words[-1].lstrip("/")
                        for source in words[:-1]:
                            src = origin / source.lstrip("/")
                            target = (
                                dest / src.name
                                if len(words) > 2 or words[-1].endswith("/")
                                else dest
                            )
                            target.parent.mkdir(parents=True, exist_ok=True)
                            if src.is_dir():
                                shutil.copytree(
                                    src, target, dirs_exist_ok=True, symlinks=True
                                )
                            else:
                                shutil.copy2(src, target)
                    elif op == "WORKDIR":
                        cwd = stage_root / value.lstrip("/")
                    elif op == "RUN":
                        command = value
                        mounts = []
                        while command.startswith("--mount="):
                            option, command = command.split(None, 1)
                            mounts.append(
                                dict(
                                    item.split("=", 1) for item in option[8:].split(",")
                                )
                            )
                        if name == "builder" and not mounts:
                            continue
                        for mount in mounts:
                            if (
                                mount.get("type") == "secret"
                                and mount.get("id") in secrets
                            ):
                                target = stage_root / mount["target"].lstrip("/")
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_text(secrets[mount["id"]])
                        command = (
                            command.replace(
                                "/home/frappe/", str(stage_root / "home/frappe") + "/"
                            )
                            .replace(
                                "/opt/frappe/", str(stage_root / "opt/frappe") + "/"
                            )
                            .replace(
                                "/usr/local/bin/",
                                str(stage_root / "usr/local/bin") + "/",
                            )
                        )
                        run = subprocess.run(
                            ["/bin/sh", "-ec", command],
                            cwd=cwd,
                            env=env,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(run.returncode, 0, run.stderr)
            events = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(
                [e[:2] for e in events],
                [
                    ["bench", "init"],
                    ["python3", "pins"],
                    ["sh", "erpnextswiss"],
                    ["sh", "frappe"],
                    ["sh", "hrms"],
                    ["bench", "build"],
                    ["pip", "bi_finance"],
                    ["pip", "bi_payroll"],
                ],
            )
            for path in (
                "apps/erpnextswiss/erpnextswiss/erpnextswiss/workspace",
                "apps/erpnextswiss/erpnextswiss/desktop_icon",
                "apps/erpnextswiss/erpnextswiss/workspace_sidebar",
            ):
                self.assertNotIn(path, events[5][2])
            final = outputs["finance"] / "home/frappe/frappe-bench"
            for name in ("gate-tests.sh", "gate-modules.sh"):
                self.assertEqual(
                    (final / "finance/scripts" / name).read_bytes(),
                    (SCRIPTS / name).read_bytes(),
                )
            for app in ("bi_finance", "bi_payroll"):
                source = ROOT / "finance/apps" / app
                for path in source.rglob("*"):
                    if path.is_file():
                        copied = final / "apps" / app / path.relative_to(source)
                        self.assertEqual(
                            copied.read_bytes(), path.read_bytes(), str(copied)
                        )


class GateCheckoutRunner(unittest.TestCase):
    def test_checkout_gate_invokes_shared_runner_without_skipping_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            (root / ".yardr").mkdir()
            (root / "finance/scripts").mkdir(parents=True)
            shutil.copy(ROOT / ".yardr/check", root / ".yardr/check")
            (root / ".yardr/check-host.sh").write_text("exit 0\n")
            (root / "finance/scripts/gate-image.sh").write_text(
                "echo finance-gate:fixture\n"
            )
            (root / "finance/scripts/test_gate_check.py").write_text("")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls"
            for command, body in (
                ("docker", 'printf "%s\\n" "$@" > "$HOME/calls"\n'),
                ("pre-commit", "exit 0\n"),
            ):
                stub = bin_dir / command
                stub.write_text("#!/bin/sh\n" + body)
                stub.chmod(0o755)
            (bin_dir / "python3").symlink_to(sys.executable)
            (bin_dir / "python").symlink_to(sys.executable)
            run = subprocess.run(
                ["sh", ".yardr/check"],
                cwd=root,
                env={"HOME": str(root), "PATH": str(bin_dir) + os.pathsep + os.defpath},
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            args = calls.read_text().splitlines()
            self.assertEqual(args[:2], ["run", "--rm"])
            self.assertEqual(
                args[-3:],
                [
                    "finance-gate:fixture",
                    "sh",
                    "/home/frappe/frappe-bench/finance/scripts/gate-tests.sh",
                ],
            )
            self.assertIn(
                str(root / "finance/scripts")
                + ":/home/frappe/frappe-bench/finance/scripts:ro",
                args,
            )


if __name__ == "__main__":
    unittest.main()

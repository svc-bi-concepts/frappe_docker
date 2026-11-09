#!/usr/bin/env python3
"""Run the three Coolify dev checks and retain each result, even after failures."""

import argparse
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", required=True, help="finance image built from this SHA"
    )
    parser.add_argument("--results", required=True, type=pathlib.Path)
    args = parser.parse_args()
    # Resolve before moving to the checkout root so the caller owns the output path.
    output = args.results.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    # Clear any previous run before executing a command; stale green results are not evidence.
    output.write_text(json.dumps(results) + "\n")
    commands = (
        (
            "frappe-tests",
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                args.image,
                "/home/frappe/frappe-bench/finance/scripts/gate-tests.sh",
            ],
        ),
        ("host-tests", ["sh", ".yardr/check-host.sh"]),
        ("lint", ["pre-commit", "run", "--color=always", "--all-files"]),
    )
    for name, command in commands:
        print(f"dev-gate: starting {name}", flush=True)
        try:
            env = dict(os.environ)
            if name == "lint":
                env["GO111MODULE"] = "on"
                env.pop("SKIP", None)
            code = subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
            # Shell-compatible exit status for a subprocess killed by a signal.
            if code < 0:
                code = 128 - code
            summary = f"{'PASS' if code == 0 else 'FAIL'} (exit {code})"
        except OSError as error:
            code = 127 if isinstance(error, FileNotFoundError) else 126
            summary = f"FAIL ({error})"
        result = {"name": name, "exit_code": code, "summary": summary}
        results.append(result)
        output.write_text(json.dumps(results, indent=2) + "\n")
        print("dev-gate-result: " + json.dumps(result), flush=True)
    return int(any(result["exit_code"] for result in results))


if __name__ == "__main__":
    raise SystemExit(main())

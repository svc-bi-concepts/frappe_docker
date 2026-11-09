"""Fail closed before any dev service starts; preserve restored site credentials."""

import json
import os
import re
import tempfile
from pathlib import Path

SWITCHES = {
    "FINANCE_MUTE_EMAILS": "mute_emails",
    "FINANCE_PAUSE_SCHEDULER": "pause_scheduler",
    "FINANCE_DISABLE_EXTERNAL_INTEGRATIONS": "disable_external_integrations",
}


def initialize(path, environ):
    for name in SWITCHES:
        if environ.get(name) != "1":
            raise ValueError(f"{name} must be exactly 1")
    if not re.fullmatch(
        r"[^\s@]+@sha256:[0-9a-f]{64}", environ.get("FINANCE_DEV_IMAGE", "")
    ):
        raise ValueError("FINANCE_DEV_IMAGE must be repository@sha256:<64 hex digits>")

    # A missing or malformed restored config must never turn into a fresh site.
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("site_config.json must contain an object")
    config.update({key: 1 for key in SWITCHES.values()})
    # Replace once, after all validation; interruption cannot leave partial JSON.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    initialize(
        Path("/home/frappe/frappe-bench/sites/frontend/site_config.json"), os.environ
    )

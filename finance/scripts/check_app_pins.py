"""Verify fetched app revisions before patches or removal of their git metadata."""

import json
import pathlib
import subprocess
import sys
from urllib.parse import urlsplit


def check(apps_file, apps_dir):
    with open(apps_file) as source:
        apps = json.load(source)
    for app in apps:
        if not app.get("commit"):
            continue
        name = pathlib.PurePosixPath(urlsplit(app["url"]).path).name.removesuffix(
            ".git"
        )
        head = subprocess.check_output(
            ["git", "-C", str(pathlib.Path(apps_dir) / name), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        if head != app["commit"]:
            raise SystemExit(f"fetched app {name} differs from its pinned commit")


if __name__ == "__main__":
    check(*sys.argv[1:])

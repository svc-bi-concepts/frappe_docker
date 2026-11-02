"""The read-only check of D4 (finance/docs/operating.md, "Monthly update (D4)"): which pinned
ERPNext apps have a newer patch release of their minor, and which image runs live. Nothing is
written and no docker runs; the upstream tags come from git ls-remote. update-check.sh runs it.

usage: update_check.py [--minor]
  --minor  also show the newest v16 minor. A minor move is a decision for ledgerdemain, not a
           monthly step, so it is only printed here, never applied.
"""

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FRAPPE_TAG = re.compile(r"^FRAPPE_TAG=(\S+)\s*$", re.M)
LIVE = re.compile(r"^\s*image:\s*frappe-finance-custom:(\S+)\s*$", re.M)
UPSTREAM = {
    "frappe": "https://github.com/frappe/frappe",
    "erpnext": "https://github.com/frappe/erpnext",
    "hrms": "https://github.com/frappe/hrms",
}


def frappe_pin(build_image_text):
    m = FRAPPE_TAG.search(build_image_text)
    if not m:
        raise ValueError("no FRAPPE_TAG in finance/scripts/build-image.sh")
    return m.group(1)


def app_pins(apps_json_text):
    """apps.json as {name: entry}, the name being the last part of the repository URL."""
    return {
        entry["url"].rstrip("/").rsplit("/", 1)[1]: entry
        for entry in json.loads(apps_json_text)
    }


def live_tag(compose_text):
    tags = sorted(set(LIVE.findall(compose_text)))
    return ", ".join(tags) if tags else "none"


def version(tag):
    m = VERSION.match(tag)
    return tuple(int(part) for part in m.groups()) if m else None


def fmt(v):
    return "v{}.{}.{}".format(*v)


def parse_ls_remote_tags(text):
    """Tag names from `git ls-remote --tags` output; the peeled ^{} entries are the same tags."""
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) != 2
            or not parts[1].startswith("refs/tags/")
            or parts[1].endswith("^{}")
        ):
            continue
        names.add(parts[1][len("refs/tags/") :])
    return names


def git_url(url):
    """Give the av git helper the .git URL form it accepts for GitHub repositories."""
    url = url.rstrip("/")
    return url if url.endswith(".git") else url + ".git"


def ls_remote_tags(url):
    out = subprocess.run(
        ["git", "ls-remote", "--tags", git_url(url)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return parse_ls_remote_tags(out.stdout)


def ls_remote_branch(url, branch):
    out = subprocess.run(
        ["git", "ls-remote", git_url(url), "refs/heads/" + branch],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    first = out.stdout.split()
    return first[0] if first else None


def tagged_line(name, pinned, tags, minor):
    """One line for an app pinned to a v16 tag: its newest patch of the same minor, and with
    --minor the newest v16 minor when it is newer."""
    pv = version(pinned)
    if pv is None:
        return f"{name:<13} pinned {pinned}  not a v16 release tag: unknown"
    releases = [v for v in (version(t) for t in tags) if v and v[0] == pv[0]]
    newest_patch = max([v for v in releases if v[:2] == pv[:2]] + [pv])
    status = "update" if newest_patch > pv else "current"
    line = f"{name:<13} pinned {pinned}  newest patch {fmt(newest_patch)}  {status}"
    if minor:
        newest_minor = max(releases + [pv])
        if newest_minor[:2] > pv[:2]:
            line += f"  newest minor {fmt(newest_minor)}  minor: ask ledgerdemain"
    return line


def main(
    argv=None,
    root=ROOT,
    remote_tags=ls_remote_tags,
    remote_branch=ls_remote_branch,
    out=sys.stdout,
):
    parser = argparse.ArgumentParser(
        description="Which pinned ERPNext apps have a newer patch release. Writes nothing."
    )
    parser.add_argument(
        "--minor",
        action="store_true",
        help="also show the newest v16 minor (a decision, not a monthly step)",
    )
    args = parser.parse_args(argv)

    def read(rel):
        with open(os.path.join(root, rel)) as f:
            return f.read()

    frappe = frappe_pin(read("finance/scripts/build-image.sh"))
    apps = app_pins(read("finance/apps.json"))
    lines = []
    for name in ("frappe", "erpnext", "hrms"):
        pinned = frappe if name == "frappe" else apps[name]["branch"]
        try:
            lines.append(
                tagged_line(name, pinned, remote_tags(UPSTREAM[name]), args.minor)
            )
        except (subprocess.SubprocessError, OSError) as err:
            lines.append(
                f"{name:<13} pinned {pinned}  unknown: upstream not reached ({type(err).__name__})"
            )
    erp = apps["erpnextswiss"]
    try:
        head = remote_branch(erp["url"], erp["branch"])
        status = "current" if head == erp.get("commit") else "update"
        lines.append(
            "{:<13} pinned branch {} at {}  newest on {} {}  {}".format(
                "erpnextswiss",
                erp["branch"],
                erp.get("commit", "-")[:7],
                erp["branch"],
                (head or "-")[:7],
                status,
            )
        )
    except (subprocess.SubprocessError, OSError) as err:
        lines.append(
            "{:<13} unknown: upstream not reached ({})".format(
                "erpnextswiss", type(err).__name__
            )
        )
    lines.append(
        "{:<13} {} (finance-local.yml)".format(
            "live", live_tag(read("finance-local.yml"))
        )
    )
    out.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

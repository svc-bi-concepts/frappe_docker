#!/usr/local/bin/av inject +SP_CERT_KEY /usr/bin/python3 -I
"""Read-only SharePoint reader for the documents payroll needs.

Talks to Microsoft Graph using a client-credentials token
from a JWT client assertion signed with the app's private key,
then GET requests for folders and file contents. Nothing else leaves this module:
every request goes through Graph._send, which allows GET to Graph and the token
POST, and nothing else.

The tenant, client, certificate thumbprint, drive and folder ids come from
private/graph-cert/sharepoint.json, which must be mode 600. The private key
comes from the environment (SP_CERT_KEY, injected by Automic Vault). It reaches
openssl through a pipe, so it is never written to disk, logged or put in an error.

    ./sp_read.py list Insurance        (from finance/sharepoint; the shebang runs it under av inject)
    finance/sharepoint/sp_read.py fetch

Standard library only, plus the openssl binary, so it runs on the system Python.
"""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"
CONFIG = os.path.join(PRIVATE, "graph-cert", "sharepoint.json")
OUT = os.path.join(PRIVATE, "payroll-insurance")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# The insurer sheets payroll needs: the BVG plan sheet, and one sheet each for UVG, NBU, KTG and FAK.
SHEETS = ("BVG", "UVG", "NBU", "KTG", "FAK")

# The folders searched for the insurer sheets, by the names they have in the config. Insurance
# first; Finance holds the rest (the sheets are somewhere under it, per Benchi, 2026-10-11).
SEARCHED = ("Insurance", "Finance")

# A sheet's token, found anywhere in a path part as long as no letter follows it: a token glued
# to the insurer's name counts (VersichererKTG_Vertrag), and so does one at a word edge, but a
# letter right after keeps a Faktura from passing for the FAK sheet.
TOKEN = {sheet: re.compile(r"%s(?![A-Za-z])" % sheet, re.I) for sheet in SHEETS}


def token_hits(sheet, folder, name):
    """True when a file's path carries the sheet's token: in the file's own name, glued or not,
    or in one of its folder names (the KTG contract sits in a folder that names KTG and a file
    that glues the insurer to the token, so neither the old word-edge rule alone nor names only
    find it).
    """
    return bool(TOKEN[sheet].search(name) or any(TOKEN[sheet].search(part) for part in folder))


class SharePointError(Exception):
    pass


def refusal_code(err):
    """Graph's error code (or the token endpoint's), never its message: a message can carry ids."""
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
    except ValueError:
        return "no code"
    code = body.get("error")
    if isinstance(code, dict):
        code = code.get("code")
    return str(code or "no code")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by http() itself: urllib would send the bearer token to the new host."""

    def redirect_request(self, *args, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method, url, headers, data, hops=3):
    """The one place a request leaves this module. Returns the body.

    Graph answers a file's /content with a redirect to a pre-authenticated download
    URL. That one is fetched with GET and without the Authorization header, over https only.
    """
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        location = err.headers.get("Location") if err.headers else None
        if method == "GET" and err.code in (301, 302, 303, 307, 308) and location:
            if hops <= 0 or not location.startswith("https://"):
                raise SharePointError("GET redirected somewhere it will not follow")
            return http("GET", location, {}, None, hops - 1)
        raise SharePointError("%s refused with HTTP %d (%s)" % (method, err.code, refusal_code(err)))
    except urllib.error.URLError as err:
        raise SharePointError("%s failed: %s" % (method, err.reason))


def load_config(path=CONFIG):
    """The config as a dict. Refused unless only its owner can read it: it holds the ids."""
    if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SharePointError("%s is readable by others; chmod 600 it" % path)
    with open(path) as f:
        return json.load(f)


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def compact(obj):
    return json.dumps(obj, separators=(",", ":")).encode()


def sign_rs256(data, key_pem):
    """The RS256 signature of data, made by openssl. The key goes in through a pipe, never a file."""
    key = key_pem.encode() if isinstance(key_pem, str) else key_pem
    r, w = os.pipe()
    try:
        if os.write(w, key) != len(key):
            raise SharePointError("the key did not fit in the pipe")
    finally:
        os.close(w)
    try:
        proc = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", "/dev/fd/%d" % r],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(r,))
    finally:
        os.close(r)
    if proc.returncode:
        raise SharePointError("signing failed: openssl exit %d" % proc.returncode)
    return proc.stdout


class Graph:
    """Reads Graph as the yardr-finance app. Every request goes through _send."""

    def __init__(self, tenant, client_id, thumbprint, drive, key_pem, transport=http, now=time.time):
        self.token_url = "%s/%s/oauth2/v2.0/token" % (LOGIN, tenant)
        self.client_id = client_id
        self.drive = drive
        try:
            self.thumbprint = bytes.fromhex(thumbprint)
        except (TypeError, ValueError):
            raise SharePointError("the certificate thumbprint must be SHA-1 hex")
        if len(self.thumbprint) != hashlib.sha1().digest_size:
            raise SharePointError("the certificate thumbprint must be SHA-1 hex")
        self.key_pem = key_pem
        self.transport = transport
        self.now = now
        self.token = None

    def _send(self, method, url, headers, data=None):
        reads_graph = method == "GET" and url.startswith(GRAPH + "/")
        gets_token = method == "POST" and url == self.token_url
        if not (reads_graph or gets_token):
            raise SharePointError("refused %s: this client only reads Graph" % method)
        return self.transport(method, url, headers, data)

    def client_assertion(self):
        """The JWT that proves the app holds the certificate's key."""
        now = int(self.now())
        header = {"alg": "RS256", "typ": "JWT", "x5t": b64url(self.thumbprint)}
        claims = {"aud": self.token_url, "iss": self.client_id, "sub": self.client_id,
                  "jti": str(uuid.uuid4()), "nbf": now, "exp": now + 300}
        signing = "%s.%s" % (b64url(compact(header)), b64url(compact(claims)))
        return "%s.%s" % (signing, b64url(sign_rs256(signing.encode(), self.key_pem)))

    def access_token(self):
        if self.token is None:
            form = urllib.parse.urlencode({
                "client_id": self.client_id,
                "scope": SCOPE,
                "grant_type": "client_credentials",
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": self.client_assertion(),
            }).encode()
            body = self._send("POST", self.token_url,
                              {"Content-Type": "application/x-www-form-urlencoded"}, form)
            self.token = json.loads(body.decode())["access_token"]
        return self.token

    def get(self, url):
        return self._send("GET", url, {"Authorization": "Bearer " + self.access_token()})

    def children(self, item_id):
        """Every child of a folder item, following Graph's paging."""
        url = "%s/drives/%s/items/%s/children" % (GRAPH, self.drive, item_id)
        items = []
        while url:
            page = json.loads(self.get(url).decode())
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
        return items

    def walk(self, item_id, path=()):
        """(folder path, file item) for every file under a folder, depth first."""
        for item in self.children(item_id):
            if "folder" in item:
                for found in self.walk(item["id"], path + (item["name"],)):
                    yield found
            elif "file" in item:
                yield path, item

    def download(self, item_id):
        return self.get("%s/drives/%s/items/%s/content" % (GRAPH, self.drive, item_id))


def pick_sheets(files):
    """({sheet: (folder path, item)}, {sheet: reason}): the sheets found exactly once, and why each other one is not.

    Nothing is guessed: no match or several is a reason, and that sheet is not downloaded. A
    file counts for a sheet when token_hits() finds the token in its path. KTG has two
    contracts in the folder (the application and the refusal are not the contract), so only a
    name with 'Vertrag' counts for it.
    """
    chosen, problems = {}, {}
    for sheet in SHEETS:
        matches = [(folder, item) for folder, item in files if token_hits(sheet, folder, item["name"])]
        if sheet == "KTG":
            matches = [(folder, item) for folder, item in matches if "vertrag" in item["name"].lower()]
        if len(matches) == 1:
            chosen[sheet] = matches[0]
        else:
            names = ", ".join(item["name"] for _, item in matches) or "none"
            problems[sheet] = "%d files match, want exactly 1 (%s)" % (len(matches), names)
    return chosen, problems


def target(out_dir, folder, name):
    """Where a file lands under out_dir, keeping its folder names. Names that could leave out_dir are refused."""
    for part in folder + (name,):
        if part in ("", ".", "..") or "/" in part or "\0" in part:
            raise SharePointError("refusing a name that is not a plain file name")
    return os.path.join(out_dir, *(folder + (name,)))


def save(path, body):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(body)
    os.chmod(path, 0o600)


def list_folder(graph, folder_id, out=sys.stdout):
    """Names and sizes of every file under a folder, recursively."""
    count = 0
    for folder, item in graph.walk(folder_id):
        out.write("%10d  %s\n" % (item.get("size", 0), "/".join(folder + (item["name"],))))
        count += 1
    out.write("%d files\n" % count)


def fetch(graph, roots, out_dir=OUT, out=sys.stdout):
    """Download every insurer sheet found exactly once under roots [(name, folder id)]; report the rest.

    A sheet that is missing or ambiguous is written to out and skipped; the others are still saved.
    Every name is checked before the first download, so a refused name leaves nothing behind.
    Each file keeps its folder names under its root's name, so two roots cannot collide.
    """
    files = []
    for name, folder_id in roots:
        files.extend(((name,) + folder, item) for folder, item in graph.walk(folder_id))
    chosen, problems = pick_sheets(files)
    plan = {}
    for sheet in SHEETS:
        if sheet in chosen:
            folder, item = chosen[sheet]
            plan[sheet] = (target(out_dir, folder, item["name"]), item)
    for sheet in SHEETS:
        if sheet in problems:
            out.write("MISSING %-4s %s\n" % (sheet, problems[sheet]))
    for sheet in SHEETS:
        if sheet not in plan:
            continue
        path, item = plan[sheet]
        body = graph.download(item["id"])
        if len(body) != item.get("size", len(body)):
            raise SharePointError("%s: downloaded size differs from the listing" % sheet)
        save(path, body)
        out.write("%-4s %10d  %s\n" % (sheet, len(body), os.path.relpath(path, out_dir)))
    out.write("%d of %d sheets saved under %s\n" % (len(plan), len(SHEETS), out_dir))


def read_paths(path):
    """The paths in a list file, one per line: '/'-joined, each starting with its root's name. Blank lines and '#' lines are skipped."""
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def fetch_paths(graph, roots, wanted, out_dir=OUT, out=sys.stdout):
    """Download the files named by wanted, each path as '/'-joined names under its root; nothing else.

    The names come from a list kept under private/, so they never reach the repo. Every path
    must match exactly one file under roots, and every name must be plain, before the first
    download: one path that is missing leaves nothing behind.
    """
    files = {}
    for name, folder_id in roots:
        for folder, item in graph.walk(folder_id):
            files.setdefault("/".join((name,) + folder + (item["name"],)), ((name,) + folder, item))
    missing = [path for path in wanted if path not in files]
    if missing:
        raise SharePointError("%d of %d paths are not under the roots; nothing downloaded" % (len(missing), len(wanted)))
    plan = [(target(out_dir, files[path][0], files[path][1]["name"]), files[path][1]) for path in wanted]
    for path, item in plan:
        body = graph.download(item["id"])
        if len(body) != item.get("size", len(body)):
            raise SharePointError("%s: downloaded size differs from the listing" % item["name"])
        save(path, body)
        out.write("%10d  %s\n" % (len(body), os.path.relpath(path, out_dir)))
    out.write("%d of %d paths saved under %s\n" % (len(plan), len(wanted), out_dir))


def main(argv, out=sys.stdout):
    if len(argv) < 2 or argv[1] not in ("list", "fetch", "fetch-paths") or (argv[1] == "list" and len(argv) != 3) \
            or (argv[1] == "fetch-paths" and len(argv) != 3):
        sys.stderr.write("usage: sp_read.py list <folder> | fetch | fetch-paths <list file>\n")
        return 2
    try:
        cfg = load_config()
        key = os.environ.get("SP_CERT_KEY")
        if not key:
            raise SharePointError("SP_CERT_KEY is not set: run this through Automic Vault")
        graph = Graph(cfg["tenant_id"], cfg["client_id"], cfg["thumbprint"], cfg["drive_id"], key)
        if argv[1] == "list":
            list_folder(graph, cfg["folders"][argv[2]], out)
        elif argv[1] == "fetch-paths":
            fetch_paths(graph, [("Finance", cfg["folders"]["Finance"])], read_paths(argv[2]), OUT, out)
        else:
            fetch(graph, [(name, cfg["folders"][name]) for name in SEARCHED], OUT, out)
    except SharePointError as err:
        sys.stderr.write("sp_read: %s\n" % err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

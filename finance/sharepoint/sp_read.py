"""Read-only SharePoint reader for the documents payroll needs.

Talks to Microsoft Graph as IT's app yardr-finance: a client-credentials token
from the app's certificate (a JWT client assertion signed with its private key),
then GET requests for folders and file contents. Nothing else leaves this module:
every request goes through Graph._send, which allows GET to Graph and the token
POST, and nothing else.

The tenant, client and drive ids and the folder item ids come from
private/graph-cert/sharepoint.json, which must be mode 600. The public
certificate is private/graph-cert/finance.pem. The private key comes from the
environment (SP_CERT_KEY, injected by varlock at run time). It reaches openssl
through a pipe, so it is never written to disk, logged or put in an error.

    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/sharepoint/sp_read.py list Insurance
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/sharepoint/sp_read.py fetch

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
CERT = os.path.join(PRIVATE, "graph-cert", "finance.pem")
OUT = os.path.join(PRIVATE, "payroll-insurance")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# The insurer sheets payroll needs: the BVG plan sheet, and one sheet each for UVG, NBU, KTG and FAK.
SHEETS = ("BVG", "UVG", "NBU", "KTG", "FAK")

# The folder that holds the insurer sheets, by the name it has in the config.
INSURANCE = "Insurance"


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


def cert_der(cert_pem):
    """The DER bytes of the first certificate in a PEM text."""
    m = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", cert_pem, re.S)
    if not m:
        raise SharePointError("no certificate in the certificate file")
    return base64.b64decode("".join(m.group(1).split()))


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
            ["openssl", "dgst", "-sha256", "-sign", "/dev/fd/%d" % r],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(r,))
    finally:
        os.close(r)
    if proc.returncode:
        raise SharePointError("signing failed: openssl exit %d" % proc.returncode)
    return proc.stdout


class Graph:
    """Reads Graph as the yardr-finance app. Every request goes through _send."""

    def __init__(self, tenant, client_id, drive, cert_pem, key_pem, transport=http, now=time.time):
        self.token_url = "%s/%s/oauth2/v2.0/token" % (LOGIN, tenant)
        self.client_id = client_id
        self.drive = drive
        self.der = cert_der(cert_pem)
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
        header = {"alg": "RS256", "typ": "JWT", "x5t#S256": b64url(hashlib.sha256(self.der).digest())}
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
    """{sheet: (folder path, item)}, one file per insurer sheet. Nothing is guessed: no match or several is an error."""
    chosen = {}
    for sheet in SHEETS:
        pattern = re.compile(r"(?<![A-Za-z])%s(?![A-Za-z])" % sheet, re.I)
        matches = [(folder, item) for folder, item in files if pattern.search(item["name"])]
        if len(matches) != 1:
            names = ", ".join(item["name"] for _, item in matches) or "none"
            raise SharePointError("%s: %d files match, want exactly 1 (%s)" % (sheet, len(matches), names))
        chosen[sheet] = matches[0]
    return chosen


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


def fetch(graph, insurance_id, out_dir=OUT, out=sys.stdout):
    """Download the insurer sheets. Every name is checked before the first download, so a refusal leaves nothing behind."""
    chosen = pick_sheets(list(graph.walk(insurance_id)))
    plan = {}
    for sheet, (folder, item) in chosen.items():
        plan[sheet] = (target(out_dir, folder, item["name"]), item)
    for sheet in SHEETS:
        path, item = plan[sheet]
        body = graph.download(item["id"])
        if len(body) != item.get("size", len(body)):
            raise SharePointError("%s: downloaded size differs from the listing" % sheet)
        save(path, body)
        out.write("%-4s %10d  %s\n" % (sheet, len(body), os.path.relpath(path, out_dir)))
    out.write("%d sheets saved under %s\n" % (len(plan), out_dir))


def main(argv, out=sys.stdout):
    if len(argv) < 2 or argv[1] not in ("list", "fetch") or (argv[1] == "list" and len(argv) != 3):
        sys.stderr.write("usage: sp_read.py list <folder> | fetch\n")
        return 2
    try:
        cfg = load_config()
        key = os.environ.get("SP_CERT_KEY")
        if not key:
            raise SharePointError("SP_CERT_KEY is not set: run this through varlock")
        with open(CERT) as f:
            cert = f.read()
        graph = Graph(cfg["tenant_id"], cfg["client_id"], cfg["drive_id"], cert, key)
        if argv[1] == "list":
            list_folder(graph, cfg["folders"][argv[2]], out)
        else:
            fetch(graph, cfg["folders"][INSURANCE], OUT, out)
    except SharePointError as err:
        sys.stderr.write("sp_read: %s\n" % err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

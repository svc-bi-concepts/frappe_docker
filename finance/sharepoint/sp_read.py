"""Read-only SharePoint reader for the documents payroll needs.

Talks to Microsoft Graph as IT's app yardr-finance. The app's key never reaches
this module: IT's token broker holds it and hands out access tokens only. A GET
to the broker on 127.0.0.1, with the broker's bearer token in the Authorization
header (read from a file at run time, never logged), returns {access_token,
expires_in} for Graph. Then GET requests for folders and file contents. Nothing
else leaves this module: every request goes through Graph._send, which allows GET
to Graph and GET to the broker, and nothing else.

The drive id and the folder item ids come from private/graph-cert/sharepoint.json,
which must be mode 600. No varlock is needed to run it.

    python3 finance/sharepoint/sp_read.py list Insurance
    python3 finance/sharepoint/sp_read.py fetch

SP_BROKER_URL and SP_BROKER_TOKEN_FILE override the broker's address (it must be
http on 127.0.0.1) and the path of its bearer token file.

Standard library only, so it runs on the system Python.
"""

import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"
CONFIG = os.path.join(PRIVATE, "graph-cert", "sharepoint.json")
OUT = os.path.join(PRIVATE, "payroll-insurance")

GRAPH = "https://graph.microsoft.com/v1.0"
BROKER = "http://127.0.0.1:18898/token/finance"
BROKER_TOKEN_FILE = "/private/var/vlbroker/graph/pub/graph-token"

# The insurer sheets payroll needs: the BVG plan sheet, and one sheet each for UVG, NBU, KTG and FAK.
SHEETS = ("BVG", "UVG", "NBU", "KTG", "FAK")

# The folder that holds the insurer sheets, by the name it has in the config.
INSURANCE = "Insurance"


class SharePointError(Exception):
    pass


def refusal_code(err):
    """Graph's error code (or the broker's), never its message: a message can carry ids."""
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


class Graph:
    """Reads Graph as the yardr-finance app, with the token from IT's broker. Every request goes through _send."""

    def __init__(self, drive, broker=BROKER, token_file=BROKER_TOKEN_FILE, transport=http):
        host = urllib.parse.urlsplit(broker)
        if host.scheme != "http" or host.hostname != "127.0.0.1":
            # The broker's bearer token goes to this address: it must not leave this machine.
            raise SharePointError("the token broker must be http on 127.0.0.1")
        self.drive = drive
        self.broker = broker
        self.token_file = token_file
        self.transport = transport
        self.token = None

    def _send(self, method, url, headers, data=None):
        reads_graph = method == "GET" and url.startswith(GRAPH + "/")
        asks_broker = method == "GET" and url == self.broker
        if not (reads_graph or asks_broker):
            raise SharePointError("refused %s: this client only reads Graph" % method)
        return self.transport(method, url, headers, data)

    def broker_token(self):
        """Graph's access token, from the broker. The bearer file is read here, at run time, and never logged."""
        try:
            with open(self.token_file) as f:
                bearer = f.read().strip()
        except OSError:
            raise SharePointError("cannot read the broker's token file")
        body = self._send("GET", self.broker, {"Authorization": "Bearer " + bearer})
        try:
            token = json.loads(body.decode()).get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not token:
            raise SharePointError("the token broker gave no access token")
        return token

    def access_token(self):
        if self.token is None:
            self.token = self.broker_token()
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
        graph = Graph(cfg["drive_id"],
                      broker=os.environ.get("SP_BROKER_URL", BROKER),
                      token_file=os.environ.get("SP_BROKER_TOKEN_FILE", BROKER_TOKEN_FILE))
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

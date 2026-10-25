#!/usr/local/bin/av inject +FINANCE_SP_CERT_KEY /bin/bash
# The Python below reaches python3 on stdin, inside this one blessed file.
# av runs a Blessed Script as /dev/fd/N, and the mini's Python 3.9 reads a
# /dev/fd script from where its zip check left the shared offset: a script
# of more than a few bytes then runs empty and exits 0 (IT, 2026-10-14).
# The heredoc takes python's stdin, so a draft's JSON goes on as fd 3.
export MAIL_STDIN_FD=3
exec /usr/bin/python3 -I - "$@" 3<&0 <<'__MAIL_PY__'
"""Benchi's mailbox for the finance yard: read it, and prepare drafts that he sends himself.

Talks to Microsoft Graph as the yardr-finance app, with the client-credentials token of the
certificate that finance/sharepoint/sp_read.py uses. Reads are GET under the mailbox. The
writes are drafts only: a new draft (POST messages), a reply draft (createReply, then PATCH of
that draft's own body) and the signature's inline images on a draft (POST attachments).
Every request goes through Graph._send, which allows those and the token POST, and nothing
else: no send, no move, no delete except the tool's own drafts, no other mailbox.

    finance/mail/mail.py list [--folder inbox] [--top 10] [--from X] [--since 2026-10-01] [--search "..."]
    finance/mail/mail.py read <id> [--attachment NAME --out DIR]
    finance/mail/mail.py draft-new          < {"subject", "to", "cc"?, "body", "contentType"?}
    finance/mail/mail.py draft-reply <id> [--all]  < {"body", "contentType"?}
    finance/mail/mail.py draft-update <id>       < {"subject"?, "body"?, "contentType"?}
    finance/mail/mail.py draft-delete <id>
    finance/mail/mail.py contacts

The mailbox, client id and tenant come from private/graph-cert/mail.json, which must be mode 600.
The private key comes from FINANCE_SP_CERT_KEY (Automic Vault) and reaches openssl through a
pipe, never a file. A draft's JSON comes on stdin only, never in argv or in a file. Output is
ids, webLinks and counts; an error names a status and a Graph error code, never a mail's content.

Exit: 0 done, 1 Graph or file error, 2 usage or missing input, 3 Contacts.Read not granted yet.
Standard library only, plus the openssl binary, so it runs on the system Python.
"""

import argparse
import base64
import datetime
import hashlib
import html
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
from html.parser import HTMLParser

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"
CONFIG = os.path.join(PRIVATE, "graph-cert", "mail.json")

# Benchi's signature as IT's it-mail-draft cut it. Read here, never written: signature.html, and
# the image files its cid: references, each named exactly as that cid.
SIGNATURE_DIR = "/Users/bsaladin/ws_yardr/integrations/state/signatures/bi-concepts"
DRAFTS_PATH = os.path.join(PRIVATE, "graph-cert", "mail-drafts.json")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# The newest mail only: one request, no paging through the mailbox (mem-v6ha).
TOP_MAX = 25
TOP_DEFAULT = 10
FOLDER_SELECT = "id,receivedDateTime,from,subject,hasAttachments,bodyPreview"

IMG_TYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
CID = re.compile(r"cid:([^\s\"'<>]+)", re.I)
CID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")  # a cid becomes a file name
MAX_IMG = 1024 * 1024

# Paths are relative to the mailbox. DELETE is additionally guarded by the tracked draft state.
DRAFT_WRITES = (
    ("POST", re.compile(r"^messages$")),
    ("POST", re.compile(r"^messages/[^/]+/(createReply|createReplyAll|attachments)$")),
    ("PATCH", re.compile(r"^messages/[^/]+$")),
    ("DELETE", re.compile(r"^messages/[^/]+$")),
)


class MailError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class GraphError(MailError):
    """Graph refused a request. Status and code only: the body can quote the mail."""

    def __init__(self, method, status, code):
        super().__init__("%s refused with HTTP %d (%s)" % (method, status, code))
        self.status = status


def refusal_code(err):
    """Graph's error code (or the token endpoint's), never its message."""
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
    except ValueError:
        return "no code"
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, dict):
        code = code.get("code")
    return str(code or "no code")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed: urllib would send the bearer token to the new host."""

    def redirect_request(self, *args, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method, url, headers, data):
    """The one place a request leaves this module. Returns the body."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        raise GraphError(method, err.code, refusal_code(err))
    except urllib.error.URLError as err:
        raise MailError("%s failed: %s" % (method, err.reason))


def load_config(path=CONFIG):
    """The config as a dict. Refused unless only its owner can read it: it holds the ids."""
    if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MailError("%s is readable by others; chmod 600 it" % path, 2)
    with open(path) as f:
        cfg = json.load(f)
    lacking = [k for k in ("tenant_id", "client_id", "thumbprint", "mailbox") if not cfg.get(k)]
    if lacking:
        raise MailError("%s lacks %s" % (path, ", ".join(lacking)), 2)
    return cfg


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
            raise MailError("the key did not fit in the pipe")
    finally:
        os.close(w)
    try:
        proc = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", "/dev/fd/%d" % r],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(r,))
    finally:
        os.close(r)
    if proc.returncode:
        raise MailError("signing failed: openssl exit %d" % proc.returncode)
    return proc.stdout


def quote_id(value):
    return urllib.parse.quote(value, safe="")


def odata_str(value):
    """An OData string literal: quotes doubled."""
    return "'%s'" % value.replace("'", "''")


def one_line(text):
    return " ".join((text or "").split())


def addr(recipient):
    """The address of a Graph recipient or from field, its name when it has none."""
    e = (recipient or {}).get("emailAddress") or {}
    return e.get("address") or e.get("name") or ""


class Graph:
    """Reads and drafts in the mailbox as yardr-finance. Every request goes through _send."""

    def __init__(self, cfg, key_pem, transport=http, now=time.time, drafts_path=DRAFTS_PATH):
        self.token_url = "%s/%s/oauth2/v2.0/token" % (LOGIN, cfg["tenant_id"])
        self.client_id = cfg["client_id"]
        self.prefix = "%s/users/%s/" % (GRAPH, quote_id(cfg["mailbox"]))
        try:
            self.thumbprint = bytes.fromhex(cfg["thumbprint"])
        except (TypeError, ValueError):
            raise MailError("the certificate thumbprint must be SHA-1 hex", 2)
        if len(self.thumbprint) != hashlib.sha1().digest_size:
            raise MailError("the certificate thumbprint must be SHA-1 hex", 2)
        self.key_pem = key_pem
        self.transport = transport
        self.now = now
        self.drafts_path = drafts_path
        self.token = None

    def _send(self, method, url, headers, data=None):
        if method == "POST" and url == self.token_url:
            return self.transport(method, url, headers, data)
        if not url.startswith(self.prefix):
            raise MailError("refused %s: only the configured mailbox is reached" % method)
        rel = url[len(self.prefix):].split("?", 1)[0]
        if method == "DELETE" and re.fullmatch(r"messages/([^/]+)", rel):
            did = urllib.parse.unquote(rel.split("/", 1)[1])
            if not any(d.get("id") == did for d in load_drafts(self.drafts_path)):
                raise MailError("refused DELETE: draft is not tracked as this tool's own", 2)
        if method == "GET" or any(method == m and pat.fullmatch(rel) for m, pat in DRAFT_WRITES):
            return self.transport(method, url, headers, data)
        raise MailError("refused %s: this client reads mail and writes drafts only" % method)

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

    def url(self, rel, query=None):
        q = "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote) if query else ""
        return self.prefix + rel + q

    def call(self, method, url, payload=None, search=False):
        headers = {"Authorization": "Bearer " + self.access_token()}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        if search:
            headers["ConsistencyLevel"] = "eventual"
        raw = self._send(method, url, headers, data)
        return json.loads(raw.decode()) if raw.strip() else {}

    def messages(self, folder, top, filters, search):
        """The newest top messages of a folder, one request: Graph's next link is never followed."""
        query = {"$top": str(top), "$select": FOLDER_SELECT}
        if search:
            query["$search"] = '"%s"' % search
        else:
            query["$orderby"] = "receivedDateTime desc"
            if filters:
                query["$filter"] = " and ".join(filters)
        rel = "mailFolders/%s/messages" % quote_id(folder)
        return self.call("GET", self.url(rel, query), search=bool(search)).get("value") or []

    def message(self, mid, select):
        return self.call("GET", self.url("messages/%s" % quote_id(mid), {"$select": select}))

    def attachments(self, mid):
        rel = "messages/%s/attachments" % quote_id(mid)
        query = {"$select": "id,name,size,contentType,isInline"}
        return self.call("GET", self.url(rel, query)).get("value") or []

    def attachment(self, mid, aid):
        return self.call("GET", self.url("messages/%s/attachments/%s" % (quote_id(mid), quote_id(aid))))

    def contacts(self):
        query = {"$top": str(TOP_MAX), "$select": "displayName,emailAddresses"}
        return self.call("GET", self.url("contacts", query)).get("value") or []

    def create_draft(self, message):
        return self.call("POST", self.url("messages"), message)

    def reply_draft(self, mid, reply_all):
        verb = "createReplyAll" if reply_all else "createReply"
        return self.call("POST", self.url("messages/%s/%s" % (quote_id(mid), verb)), {})

    def patch_draft(self, did, body):
        return self.call("PATCH", self.url("messages/%s" % quote_id(did)), {"body": body})

    def add_attachment(self, did, attachment):
        return self.call("POST", self.url("messages/%s/attachments" % quote_id(did)), attachment)

    def delete_draft(self, did):
        self.call("DELETE", self.url("messages/%s" % quote_id(did)))


def load_drafts(path=DRAFTS_PATH):
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return []
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MailError("%s is readable by others; chmod 600 it" % path, 2)
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise MailError("draft tracking state is invalid", 2)
    return rows


def save_drafts(rows, path=DRAFTS_PATH):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.exists(path) and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MailError("%s is readable by others; chmod 600 it" % path, 2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.fstat(fd).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise MailError("%s is readable by others; chmod 600 it" % path, 2)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(rows, f)
    finally:
        if fd >= 0:
            os.close(fd)


def track_draft(graph, did, kind):
    rows = [d for d in load_drafts(graph.drafts_path) if d.get("id") != did]
    rows.append({"id": did, "kind": kind,
                 "created": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    save_drafts(rows, graph.drafts_path)


def search_text(value):
    """A $search phrase between double quotes: the quotes it holds are dropped."""
    return value.replace('"', "")


def from_filter(who):
    if "@" in who:
        return "from/emailAddress/address eq %s" % odata_str(who)
    return "startswith(from/emailAddress/name,%s)" % odata_str(who)


def list_rows(graph, folder, top, from_who=None, since=None, search=None):
    if search and (from_who or since):
        raise MailError("--search does not combine with --from or --since", 2)
    filters = []
    if from_who or since:
        # Graph refuses a filter plus $orderby (InefficientFilter) unless the ordered property
        # leads the filter, so receivedDateTime is always its first term.
        filters.append("receivedDateTime ge %sT00:00:00Z" % (since or "1970-01-01"))
    if from_who:
        filters.append(from_filter(from_who))
    return graph.messages(folder, top, filters, search_text(search) if search else None)


def row_text(m):
    received = (m.get("receivedDateTime") or "")[:16].replace("T", " ")
    preview = one_line(m.get("bodyPreview"))[:100]
    return "\t".join([m.get("id", ""), received, addr(m.get("from")), one_line(m.get("subject")),
                      "att" if m.get("hasAttachments") else "", preview])


class _Text(HTMLParser):
    """HTML to plain text: the block tags become line breaks, script and style are dropped."""

    BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"}
    SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(content):
    p = _Text()
    p.feed(content)
    p.close()
    out, blank = [], False
    for line in "".join(p.parts).split("\n"):
        line = one_line(line)
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def body_text(body):
    content = (body or {}).get("content") or ""
    if ((body or {}).get("contentType") or "").lower() == "html":
        return html_to_text(content)
    return content.strip()


def check_plain_name(name):
    if name in ("", ".", "..") or os.path.basename(name) != name or "\0" in name:
        raise MailError("refusing a name that is not a plain file name", 2)


def save_attachment(graph, mid, name, out_dir):
    """The one attachment of that name, written under out_dir; never over an existing file."""
    check_plain_name(name)
    matches = [a for a in graph.attachments(mid) if a.get("name") == name]
    if len(matches) != 1:
        raise MailError("%d attachments carry that name; want exactly one" % len(matches), 2)
    one = graph.attachment(mid, matches[0]["id"])
    if not one.get("contentBytes"):
        raise MailError("that attachment is not a file (it carries no content)")
    data = base64.b64decode(one["contentBytes"])
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    path = os.path.join(out_dir, name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise MailError("%s already exists; nothing written" % path, 2)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path, len(data)


def contact_rows(graph):
    """The contacts as (name, address) pairs. A 403 is exit 3: Contacts.Read is not granted yet."""
    try:
        rows = graph.contacts()
    except GraphError as err:
        if err.status == 403:
            raise MailError("Contacts.Read is not granted to yardr-finance yet (queued at IT); "
                            "Graph refused the request", 3)
        raise
    pairs = []
    for c in rows:
        mails = [e.get("address") for e in c.get("emailAddresses") or [] if e.get("address")]
        pairs.append((one_line(c.get("displayName")), ",".join(mails)))
    return pairs


def load_signature(d=None):
    """(signature html, [(cid, bytes)]): every image the HTML references, each a safe image file of
    at most 1 MB. Checked before any Graph call, so a missing image leaves no draft behind."""
    d = d or SIGNATURE_DIR
    try:
        with open(os.path.join(d, "signature.html"), encoding="utf-8") as f:
            sig = f.read()
    except OSError:
        raise MailError("no signature.html in the signature directory", 2)
    images = []
    for cid in sorted(set(CID.findall(sig))):
        ext = os.path.splitext(cid.split("@", 1)[0])[1].lower()
        if not CID_SAFE.match(cid) or ext not in IMG_TYPE:
            raise MailError("signature cid %s is not a safe image file name" % cid, 2)
        path = os.path.join(d, cid)
        try:
            if os.path.getsize(path) > MAX_IMG:
                raise MailError("signature image %s is over 1 MB" % cid, 2)
            with open(path, "rb") as f:
                images.append((cid, f.read()))
        except OSError:
            raise MailError("signature image %s is missing" % cid, 2)
    return sig, images


def inline_attachment(cid, data):
    """A Graph fileAttachment: inline, contentId = the cid, the file name before @ as its name."""
    name = cid.split("@", 1)[0]
    return {"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
            "contentType": IMG_TYPE[os.path.splitext(name)[1].lower()],
            "contentBytes": base64.b64encode(data).decode(), "isInline": True,
            "contentId": cid}


def attach_images(graph, did, images):
    for cid, data in images:
        try:
            graph.add_attachment(did, inline_attachment(cid, data))
        except GraphError as err:
            raise MailError("draft %s stands, but its signature image did not attach (%s)"
                            % (did, err), 1)


def recipients(addrs):
    return [{"emailAddress": {"address": a}} for a in addrs]


def valid_addresses(name, value):
    if not isinstance(value, list) or not value:
        raise MailError("input: %s needs a list of addresses" % name, 2)
    for a in value:
        if not isinstance(a, str) or "@" not in a or " " in a:
            raise MailError("input: %s holds a value that is no mail address" % name, 2)
    return value


def parse_body(draft):
    if not isinstance(draft, dict) or not isinstance(draft.get("body"), str):
        raise MailError("input: JSON needs body (text)", 2)
    ctype = str(draft.get("contentType", "Text")).lower()
    if ctype not in ("text", "html"):
        raise MailError("input: contentType is Text or HTML", 2)
    return draft["body"], ctype


def body_html(text, ctype):
    """Our text as an HTML fragment: plain text escaped with its lines broken, HTML as given."""
    if ctype == "html":
        return text
    return "<div>%s</div>" % html.escape(text).replace("\n", "<br>\n")


def compose(ours_html, sig):
    """Our text, then Benchi's signature: the HTML body of a draft that is never sent as plain text."""
    return "%s\n<br>\n%s" % (ours_html, sig)


def with_block(orig, block):
    """block placed right after the reply's <body> tag, so it sits above Outlook's quoted history."""
    m = re.search(r"<body[^>]*>", orig, re.I)
    if m:
        return orig[:m.end()] + "\n" + block + orig[m.end():]
    return block + orig


def draft_ok(m, source=None):
    did = m.get("id") or ""
    if not did or did == source or m.get("isDraft") is not True:
        raise MailError("Graph did not return a new draft; nothing changed")
    return did


def draft_new(graph, draft):
    """A new draft: our text, the signature, its inline images. Returns (draft id, webLink)."""
    if not isinstance(draft, dict) or not isinstance(draft.get("subject"), str) or not draft.get("subject"):
        raise MailError("input: JSON needs subject", 2)
    to = valid_addresses("to", draft.get("to"))
    cc = valid_addresses("cc", draft["cc"]) if draft.get("cc") else None
    text, ctype = parse_body(draft)
    sig, images = load_signature()
    message = {"subject": draft["subject"],
               "body": {"contentType": "HTML", "content": compose(body_html(text, ctype), sig)},
               "toRecipients": recipients(to)}
    if cc:
        message["ccRecipients"] = recipients(cc)
    m = graph.create_draft(message)
    did = draft_ok(m)
    track_draft(graph, did, "new")
    attach_images(graph, did, images)
    return did, m.get("webLink") or ""


def draft_reply(graph, source, reply_all, draft):
    """A reply draft in the thread of source: our text, the signature, then Outlook's quoted
    history. The draft is patched only after createReply made it, and only when it is a new one."""
    text, ctype = parse_body(draft)
    sig, images = load_signature()  # before createReply: a missing image leaves no stray draft
    m = graph.reply_draft(source, reply_all)
    did = draft_ok(m, source)
    track_draft(graph, did, "reply")
    orig = (m.get("body") or {}).get("content")
    if orig is None:
        raise MailError("the reply draft has no body to keep the quoted original in")
    if ((m.get("body") or {}).get("contentType") or "html").lower() != "html":
        orig = body_html(orig, "text")  # a plain-text original, kept with its line breaks
    patched = graph.patch_draft(did, {"contentType": "HTML",
                                      "content": with_block(orig, compose(body_html(text, ctype), sig))})
    if patched.get("id") not in (None, did):
        raise MailError("Graph did not confirm the patch of draft %s" % did)
    attach_images(graph, did, images)
    return did, (patched.get("webLink") or m.get("webLink") or "")


def read_input():
    """The JSON the caller put on stdin (fd 3 under the launcher, sys.stdin when run alone)."""
    fd = os.environ.get("MAIL_STDIN_FD")
    src = os.fdopen(int(fd), closefd=False) if fd else sys.stdin
    try:
        return json.load(src)
    except ValueError:
        raise MailError("input: the JSON on stdin does not parse", 2)


def top_arg(value):
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--top takes a number")
    if not 1 <= n <= TOP_MAX:
        raise argparse.ArgumentTypeError("--top is 1 to %d: the newest mail only, never a page through the mailbox" % TOP_MAX)
    return n


def date_arg(value):
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("--since takes a date, YYYY-MM-DD")


def cmd_list(graph, args, out):
    rows = list_rows(graph, args.folder, args.top, args.from_, args.since, args.search)
    for m in rows:
        out.write(row_text(m) + "\n")


READ_SELECT = "subject,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,body"


def cmd_read(graph, args, out):
    if bool(args.attachment) != bool(args.out):
        raise MailError("--attachment and --out go together", 2)
    if args.attachment:
        path, size = save_attachment(graph, args.id, args.attachment, args.out)
        out.write("saved %s (%d bytes)\n" % (path, size))
        return
    m = graph.message(args.id, READ_SELECT)
    out.write("From: %s\n" % addr(m.get("from")))
    out.write("To: %s\n" % ", ".join(addr(r) for r in m.get("toRecipients") or []))
    if m.get("ccRecipients"):
        out.write("Cc: %s\n" % ", ".join(addr(r) for r in m["ccRecipients"]))
    out.write("Date: %s\n" % (m.get("receivedDateTime") or "")[:16].replace("T", " "))
    out.write("Subject: %s\n\n" % one_line(m.get("subject")))
    out.write(body_text(m.get("body")) + "\n")
    atts = graph.attachments(args.id)
    if atts:
        out.write("\nAttachments:\n")
        for a in atts:
            kind = "inline" if a.get("isInline") else "attached"
            out.write("  %s  %d bytes  %s\n" % (a.get("name", ""), a.get("size", 0), kind))


def cmd_draft_new(graph, args, out):
    did, link = draft_new(graph, read_input())
    out.write("draft: %s\nwebLink: %s\n" % (did, link))


def cmd_draft_reply(graph, args, out):
    did, link = draft_reply(graph, args.id, args.all, read_input())
    out.write("draft: %s\nwebLink: %s\n" % (did, link))


def guard_draft(graph, did):
    m = graph.message(did, "id,isDraft,parentFolderId")
    folder = graph.call("GET", graph.url("mailFolders/drafts", {"$select": "id"}))
    if m.get("isDraft") is not True:
        raise MailError("refused: the message is not a draft", 2)
    if not m.get("parentFolderId") or m.get("parentFolderId") != folder.get("id"):
        raise MailError("refused: the draft is not in Drafts", 2)
    return m


def draft_update(graph, did, draft):
    if not isinstance(draft, dict) or not any(k in draft for k in ("subject", "body")):
        raise MailError("input: JSON needs subject and/or body", 2)
    if "subject" in draft and not isinstance(draft["subject"], str):
        raise MailError("input: subject must be text", 2)
    record = next((d for d in load_drafts(graph.drafts_path) if d.get("id") == did), None)
    guard_draft(graph, did)
    if "body" in draft and record and record.get("kind") == "reply":
        raise MailError("refused: delete it and make a new draft-reply", 2)
    payload = {}
    if "subject" in draft:
        payload["subject"] = draft["subject"]
    if "body" in draft:
        text, ctype = parse_body(draft)
        sig, images = load_signature()
        payload["body"] = {"contentType": "HTML", "content": compose(body_html(text, ctype), sig)}
        cids = {a.get("contentId") for a in graph.attachments(did)}
    updated = graph.call("PATCH", graph.url("messages/%s" % quote_id(did)), payload)
    if "body" in draft:
        attach_images(graph, did, [(cid, data) for cid, data in images if cid not in cids])
    return updated.get("webLink") or ""


def cmd_draft_update(graph, args, out):
    link = draft_update(graph, args.id, read_input())
    out.write("draft: %s\nwebLink: %s\n" % (args.id, link))


def cmd_draft_delete(graph, args, out):
    rows = load_drafts(graph.drafts_path)
    if not any(d.get("id") == args.id for d in rows):
        raise MailError("refused: draft is not tracked as this tool's own", 2)
    guard_draft(graph, args.id)
    graph.delete_draft(args.id)
    save_drafts([d for d in rows if d.get("id") != args.id], graph.drafts_path)
    out.write("deleted draft: %s\n" % args.id)


def cmd_contacts(graph, args, out):
    for name, mails in contact_rows(graph):
        out.write("%s\t%s\n" % (name, mails))


def parser():
    p = argparse.ArgumentParser(prog="mail.py", description="Benchi's mailbox: read, and drafts only.")
    sub = p.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="the newest mail of a folder, top N only")
    ls.add_argument("--folder", default="inbox")
    ls.add_argument("--top", type=top_arg, default=TOP_DEFAULT, help="1 to 25")
    ls.add_argument("--from", dest="from_", help="an address, or a name at the start of the sender's name")
    ls.add_argument("--since", type=date_arg, help="YYYY-MM-DD, received on or after")
    ls.add_argument("--search", help="Graph's search, with no --from or --since")
    ls.set_defaults(run=cmd_list)
    rd = sub.add_parser("read", help="one mail as text, or one attachment saved")
    rd.add_argument("id")
    rd.add_argument("--attachment", help="the attachment's name, saved under --out")
    rd.add_argument("--out", help="the directory an attachment is saved into")
    rd.set_defaults(run=cmd_read)
    sub.add_parser("draft-new", help="a new draft from the JSON on stdin").set_defaults(run=cmd_draft_new)
    dr = sub.add_parser("draft-reply", help="a reply draft from the JSON on stdin (body)")
    dr.add_argument("id")
    dr.add_argument("--all", action="store_true", help="reply to all")
    dr.set_defaults(run=cmd_draft_reply)
    du = sub.add_parser("draft-update", help="update a draft from JSON on stdin")
    du.add_argument("id")
    du.set_defaults(run=cmd_draft_update)
    dd = sub.add_parser("draft-delete", help="delete a draft made by this tool")
    dd.add_argument("id")
    dd.set_defaults(run=cmd_draft_delete)
    sub.add_parser("contacts", help="the top 25 contacts (needs Contacts.Read)").set_defaults(run=cmd_contacts)
    return p


def main(argv, out=sys.stdout):
    args = parser().parse_args(argv[1:])
    try:
        cfg = load_config()
        key = os.environ.get("FINANCE_SP_CERT_KEY")
        if not key:
            raise MailError("FINANCE_SP_CERT_KEY is not set: run this through Automic Vault", 2)
        args.run(Graph(cfg, key), args, out)
    except MailError as err:
        sys.stderr.write("mail: %s\n" % err)
        return err.code
    except OSError as err:
        sys.stderr.write("mail: %s\n" % (err.strerror or "file error"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
__MAIL_PY__

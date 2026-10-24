"""Local SharePoint broker for Yardr. Secrets stay in memory; writes are output-only."""

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"
CONFIG = os.path.join(PRIVATE, "graph-cert", "sharepoint.json")
WORK = "/Users/bsaladin/ws_yardr_finance/work"
OUT_DIR = os.path.join(WORK, "out")
INBOX_DIR = os.path.join(WORK, "inbox")
ADDR = ("127.0.0.1", 8817)
SIMPLE_MAX_FILE = 4 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
MAX_FILE = 250 * 1024 * 1024
OOXML_EXTS = frozenset(
    "docx docm dotx dotm xlsx xlsm xltx xltm xlam pptx pptm potx potm ppsx ppsm".split()
)


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_config(path=CONFIG):
    if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
        raise ValueError("SharePoint config must be mode 600")
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config.get("folders"), dict) or not isinstance(
        config.get("out"), dict
    ):
        raise ValueError("SharePoint config needs folders and out entries")
    return config


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def compact(value):
    return json.dumps(value, separators=(",", ":")).encode()


def sign_rs256(data, key):
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, key.encode() if isinstance(key, str) else key)
    finally:
        os.close(write_fd)
    try:
        result = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", "/dev/fd/%d" % read_fd],
            input=data,
            capture_output=True,
            pass_fds=(read_fd,),
        )
    finally:
        os.close(read_fd)
    if result.returncode:
        raise RuntimeError("signing failed: openssl exit %d" % result.returncode)
    return result.stdout


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Graph:
    """Graph transport that pins every Graph write to the configured output item."""

    def __init__(self, config, key, transport=None):
        self.config = config
        self.key = key
        self.transport = transport or self._http
        self.token = None
        self.token_url = "{}/{}/oauth2/v2.0/token".format(LOGIN, config["tenant_id"])
        self.out = config["out"]
        self.upload_urls = set()

    @staticmethod
    def _http(method, url, headers=None, data=None, hops=3):
        request = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method
        )
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=300) as response:
                return Response(
                    response.status,
                    response.read(MAX_FILE + 1),
                    dict(response.headers.items()),
                )
        except urllib.error.HTTPError as err:
            location = err.headers.get("Location") if err.headers else None
            if method == "GET" and err.code in (301, 302, 303, 307, 308) and location:
                if hops <= 0 or not location.startswith("https://"):
                    raise Fail(502, "Graph download redirect refused")
                return Graph._http("GET", location, {}, None, hops - 1)
            return Response(err.code, err.read(), dict(err.headers.items()))

    def assertion(self):
        now = int(time.time())
        header = {
            "alg": "RS256",
            "typ": "JWT",
            "x5t": b64url(bytes.fromhex(self.config["thumbprint"])),
        }
        claims = {
            "aud": self.token_url,
            "iss": self.config["client_id"],
            "sub": self.config["client_id"],
            "jti": str(uuid.uuid4()),
            "nbf": now,
            "exp": now + 300,
        }
        signed = b64url(compact(header)) + "." + b64url(compact(claims))
        return signed + "." + b64url(sign_rs256(signed.encode(), self.key))

    def access_token(self):
        if not self.token:
            form = urllib.parse.urlencode(
                {
                    "client_id": self.config["client_id"],
                    "scope": SCOPE,
                    "grant_type": "client_credentials",
                    "client_assertion_type": ASSERTION_TYPE,
                    "client_assertion": self.assertion(),
                }
            ).encode()
            resp = self.transport(
                "POST",
                self.token_url,
                {"Content-Type": "application/x-www-form-urlencoded"},
                form,
            )
            if resp.status >= 400:
                raise Fail(502, "Graph token request failed")
            self.token = resp.json()["access_token"]
        return self.token

    def request(
        self, method, url, data=None, headers=None, json_body=None, missing_ok=False
    ):
        upload_url = url in self.upload_urls
        if method == "GET" and not url.startswith(GRAPH + "/"):
            raise Fail(403, "refused Graph read outside Graph")
        if method in ("PUT", "POST", "DELETE"):
            outbase = "{}/drives/{}/items/{}".format(
                GRAPH, self.out["drive_id"], self.out["item_id"]
            )
            output_url = url == outbase or url.startswith(outbase + ":/")
            allowed = upload_url if method == "DELETE" else (output_url or upload_url)
            if not allowed:
                raise Fail(403, "refused Graph write outside the output folder")
        hdrs = {} if upload_url else {"Authorization": "Bearer " + self.access_token()}
        hdrs.update(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        resp = self.transport(method, url, hdrs, data)
        if missing_ok and resp.status == 404:
            return None
        if resp.status >= 400:
            raise Fail(502, "Graph %s failed with HTTP %d" % (method, resp.status))
        return resp


AREAS = {}
TOKEN = ""
GRAPH_CLIENT = None


def clean(path):
    if not isinstance(path, str) or "\0" in path:
        raise Fail(400, "bad path")
    p = path.replace("\\", "/").strip("/")
    parts = [part for part in p.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts) or any(c in p for c in ':*?"<>|'):
        raise Fail(400, "bad path")
    return "/".join(parts)


def area_url(area, path=""):
    if area not in AREAS:
        raise Fail(400, "unknown area")
    a = AREAS[area]
    base = "{}/drives/{}/items/{}".format(GRAPH, a["drive_id"], a["item_id"])
    p = clean(path)
    return base + (":/%s:" % urllib.parse.quote(p, safe="/")) if p else base


def resolve_areas(client, config):
    drive_id = config["drive_id"]
    areas = {"out": config["out"]}
    areas.update(
        {
            name: {"drive_id": drive_id, "item_id": item_id}
            for name, item_id in config["folders"].items()
        }
    )
    resolved = {}
    for name, item in areas.items():
        url = "{}/drives/{}/items/{}".format(GRAPH, item["drive_id"], item["item_id"])
        metadata = client.request("GET", url).json()
        if "folder" not in metadata:
            raise ValueError("configured area is not a folder")
        parent = metadata.get("parentReference", {})
        path = parent.get("path", "").split("root:", 1)[-1] + "/" + metadata["name"]
        resolved[name] = {
            "drive_id": item["drive_id"],
            "item_id": item["item_id"],
            "path": path,
        }
    return resolved


def graph(method, url, **kwargs):
    return GRAPH_CLIENT.request(method, url, **kwargs)


def do_ping(_):
    return {"ok": True, **{name: area["path"] for name, area in AREAS.items()}}


def do_ls(body):
    url = area_url(body.get("area"), body.get("path", "")) + "/children"
    items = []
    while url:
        page = graph("GET", url).json()
        for item in page.get("value", []):
            items.append(
                {
                    "name": item["name"],
                    "folder": "folder" in item,
                    "size": item.get("size", 0),
                    "modified": item.get("lastModifiedDateTime", ""),
                }
            )
        url = page.get("@odata.nextLink")
    return {"items": items}


def local_src(src):
    if not isinstance(src, str) or not os.path.isabs(src) or "\0" in src:
        raise Fail(403, "src must be an absolute path inside the out folder")
    root = os.path.realpath(OUT_DIR)
    real = os.path.realpath(src)
    if not real.startswith(root + os.sep):
        raise Fail(403, "src is outside the out folder")
    if not os.path.isfile(real):
        raise Fail(404, "no such file in the out folder")
    return real


def inbox_path(area, path):
    root = os.path.realpath(INBOX_DIR)
    directory = os.path.realpath(os.path.join(root, area, os.path.dirname(path)))
    destination = os.path.realpath(os.path.join(directory, os.path.basename(path)))
    if not destination.startswith(root + os.sep):
        raise Fail(403, "destination is outside the inbox folder")
    return directory, destination


def do_get_file(body):
    area = body.get("area")
    p = clean(body.get("path", ""))
    if not p:
        raise Fail(400, "path is required")
    url = area_url(area, p)
    meta = graph("GET", url).json()
    if "folder" in meta:
        raise Fail(400, "is a folder")
    if meta.get("size", 0) > MAX_FILE:
        raise Fail(413, "file exceeds 250 MB")
    directory, destination = inbox_path(area, p)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    data = graph("GET", url + "/content").body
    if len(data) > MAX_FILE or len(data) != meta.get("size", len(data)):
        raise Fail(502, "download size mismatch")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".get_file-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(tmp, destination)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {
        "name": meta["name"],
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "saved": destination,
    }


def put_chunk(url, data, start, total):
    end = start + len(data) - 1
    resp = graph(
        "PUT",
        url,
        headers={"Content-Range": "bytes %d-%d/%d" % (start, end, total)},
        data=data,
    )
    return resp


def do_put_file(body):
    src = local_src(body.get("src"))
    path = clean(body.get("path", ""))
    if not path:
        raise Fail(400, "path is required")
    size = os.path.getsize(src)
    if size > MAX_FILE:
        raise Fail(413, "file exceeds 250 MB")
    replace = str(body.get("replace", "false")).lower() == "true"
    target = area_url("out", path)
    existing = graph("GET", target, missing_ok=True)
    if existing is not None and not replace:
        raise Fail(409, "file already exists; pass replace=true")
    digest = hashlib.sha256()
    if size <= SIMPLE_MAX_FILE:
        with open(src, "rb") as f:
            data = f.read()
        if len(data) != size:
            raise Fail(409, "local file changed while reading")
        digest.update(data)
        behavior = "replace" if replace else "fail"
        item = graph(
            "PUT",
            target + "/content?@microsoft.graph.conflictBehavior=" + behavior,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            json_body=None,
        ).json()
    else:
        session = graph(
            "POST",
            target + "/createUploadSession",
            json_body={
                "item": {
                    "@microsoft.graph.conflictBehavior": (
                        "replace" if replace else "fail"
                    )
                }
            },
        ).json()
        upload_url = session["uploadUrl"]
        GRAPH_CLIENT.upload_urls.add(upload_url)
        sent, item = 0, None
        try:
            with open(src, "rb") as f:
                while sent < size:
                    data = f.read(min(CHUNK, size - sent))
                    if not data:
                        raise Fail(409, "local file got shorter while uploading")
                    digest.update(data)
                    item = put_chunk(upload_url, data, sent, size)
                    sent += len(data)
            if item.status not in (200, 201):
                raise Fail(502, "upload session did not finish")
            item = item.json()
        except BaseException:
            try:
                graph("DELETE", upload_url)
            except Exception:
                pass
            raise
        finally:
            GRAPH_CLIENT.upload_urls.discard(upload_url)
    actual = item.get("size")
    modified = actual != size
    if modified and path.rsplit(".", 1)[-1].lower() not in OOXML_EXTS:
        raise Fail(502, "SharePoint file size differs from local file")
    return {
        "name": item.get("name", os.path.basename(path)),
        "size": actual,
        "sha256": digest.hexdigest(),
        "sharepoint_modified": modified,
    }


ROUTES = {
    "/ping": do_ping,
    "/ls": do_ls,
    "/get_file": do_get_file,
    "/put_file": do_put_file,
}


def authorized(header, token):
    return bool(token) and hmac.compare_digest(header, "Bearer " + token)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            auth = self.headers.get("Authorization", "")
            if not authorized(auth, TOKEN):
                raise Fail(401, "unauthorized")
            fn = ROUTES.get(self.path)
            if fn is None:
                raise Fail(404, "no such call")
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise Fail(413, "request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            self.reply(200, fn(body))
        except Fail as err:
            self.reply(err.code, {"error": str(err)})
        except Exception as err:
            self.reply(500, {"error": type(err).__name__})

    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)


def main():
    global TOKEN, GRAPH_CLIENT, AREAS
    TOKEN = os.environ.get("FINANCE_SP_BROKER_TOKEN", "")
    key = os.environ.get("FINANCE_SP_CERT_KEY", "")
    if not TOKEN or not key:
        raise SystemExit("broker credentials are missing")
    config = load_config()
    GRAPH_CLIENT = Graph(config, key)
    AREAS = resolve_areas(GRAPH_CLIENT, config)
    for name, area in AREAS.items():
        print("{}: {}".format(name, area["path"]), flush=True)
    print("listening on http://%s:%d" % ADDR, flush=True)
    ThreadingHTTPServer(ADDR, Handler).serve_forever()


if __name__ == "__main__":
    main()

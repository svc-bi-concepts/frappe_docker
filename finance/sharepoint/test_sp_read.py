"""The SharePoint reader's tests, on a fake Graph: no network, no real key or ids.

The key and certificate are throwaway ones made here with openssl, so the signing
can be checked against the public key. Standard library only, plus openssl.
"""

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import sp_read
from sp_read import GRAPH, LOGIN, Graph, SharePointError

TENANT = "tenant-x"
CLIENT = "client-x"
DRIVE = "drive-x"
TOKEN_URL = "%s/%s/oauth2/v2.0/token" % (LOGIN, TENANT)


def openssl(*args, **kw):
    return subprocess.run(["openssl"] + list(args), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, **kw).stdout


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeGraph:
    """Answers the URLs it knows; records every call it gets."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        if url not in self.routes:
            raise AssertionError("no route for %s %s" % (method, url))
        return self.routes[url]


def page(items, next_link=None):
    body = {"value": items}
    if next_link:
        body["@odata.nextLink"] = next_link
    return json.dumps(body).encode()


def folder(name, item_id):
    return {"id": item_id, "name": name, "folder": {"childCount": 1}}


def file_(name, item_id, size):
    return {"id": item_id, "name": name, "size": size, "file": {}}


class Keys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sp-read-test-")
        cls.key_path = os.path.join(cls.dir, "key.pem")
        cls.cert_path = os.path.join(cls.dir, "cert.pem")
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cls.key_path,
                "-out", cls.cert_path, "-subj", "/CN=test", "-days", "1")
        with open(cls.key_path) as f:
            cls.key_pem = f.read()
        with open(cls.cert_path) as f:
            cls.cert_pem = f.read()
        cls.cert_der = openssl("x509", "-in", cls.cert_path, "-outform", "DER")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)


class ClientTest(Keys):
    def setUp(self):
        self.token_route = {TOKEN_URL: json.dumps({"access_token": "fake-token"}).encode()}

    def graph(self, routes, now=lambda: 1000):
        fake = FakeGraph(dict(self.token_route, **routes))
        return fake, Graph(TENANT, CLIENT, DRIVE, self.cert_pem, self.key_pem, transport=fake, now=now)

    def test_assertion_is_signed_by_the_certificates_key(self):
        fake, g = self.graph({})
        header, claims, sig = g.client_assertion().split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {
            "alg": "RS256", "typ": "JWT",
            "x5t#S256": sp_read.b64url(hashlib.sha256(self.cert_der).digest())})
        body = json.loads(b64url_decode(claims))
        self.assertEqual((body["aud"], body["iss"], body["sub"]), (TOKEN_URL, CLIENT, CLIENT))
        self.assertEqual((body["nbf"], body["exp"]), (1000, 1300))

        pub = openssl("x509", "-in", self.cert_path, "-pubkey", "-noout")
        pub_path = os.path.join(self.dir, "pub.pem")
        data_path = os.path.join(self.dir, "signing.bin")
        sig_path = os.path.join(self.dir, "sig.bin")
        with open(pub_path, "wb") as f:
            f.write(pub)
        with open(data_path, "wb") as f:
            f.write((header + "." + claims).encode())
        with open(sig_path, "wb") as f:
            f.write(b64url_decode(sig))
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub_path,
                                   "-signature", sig_path, data_path],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_token_request_carries_the_assertion_not_the_key(self):
        fake, g = self.graph({})
        g.access_token()
        (method, url, _, data), = [c for c in fake.calls if c[0] == "POST"]
        self.assertEqual((method, url), ("POST", TOKEN_URL))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode()).items()}
        self.assertEqual(form["grant_type"], "client_credentials")
        self.assertEqual(form["client_assertion_type"], sp_read.ASSERTION_TYPE)
        self.assertNotIn("BEGIN", data.decode())
        self.assertNotIn(self.key_pem, repr(fake.calls))

    def test_children_follow_paging(self):
        first = GRAPH + "/drives/%s/items/root/children?page=2" % DRIVE
        fake, g = self.graph({
            GRAPH + "/drives/%s/items/ins/children" % DRIVE:
                page([file_("a.pdf", "1", 3)], next_link=first),
            first: page([file_("b.pdf", "2", 4)]),
        })
        self.assertEqual([i["name"] for i in g.children("ins")], ["a.pdf", "b.pdf"])

    def test_walk_is_recursive_and_keeps_folder_names(self):
        fake, g = self.graph({
            GRAPH + "/drives/%s/items/ins/children" % DRIVE:
                page([folder("BVG", "f1"), file_("top.pdf", "9", 1)]),
            GRAPH + "/drives/%s/items/f1/children" % DRIVE:
                page([file_("plan.xlsx", "10", 5)]),
        })
        self.assertEqual(
            [(path, item["name"]) for path, item in g.walk("ins")],
            [(("BVG",), "plan.xlsx"), ((), "top.pdf")])

    def test_only_gets_to_graph_and_the_token_post_leave(self):
        fake, g = self.graph({
            GRAPH + "/drives/%s/items/ins/children" % DRIVE: page([file_("UVG.pdf", "1", 2)]),
            GRAPH + "/drives/%s/items/1/content" % DRIVE: b"..",
        })
        self.assertEqual(g.download("1"), b"..")
        list(g.walk("ins"))
        for method, url, _, _ in fake.calls:
            self.assertTrue((method == "GET" and url.startswith(GRAPH + "/")) or
                            (method == "POST" and url == TOKEN_URL), (method, url))
        for method in ("PUT", "PATCH", "DELETE", "POST"):
            with self.assertRaises(SharePointError):
                g._send(method, GRAPH + "/drives/%s/items/1" % DRIVE, {}, b"x")
        with self.assertRaises(SharePointError):
            g._send("GET", TOKEN_URL, {})
        with self.assertRaises(SharePointError):
            g._send("POST", GRAPH + "/drives/%s/items/1/children" % DRIVE, {}, b"x")


class FetchTest(Keys):
    INS = GRAPH + "/drives/%s/items/ins/children" % DRIVE
    BVG = GRAPH + "/drives/%s/items/f-bvg/children" % DRIVE

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="sp-out-")
        self.routes = {
            TOKEN_URL: json.dumps({"access_token": "fake-token"}).encode(),
            self.INS: page([folder("BVG", "f-bvg"), file_("UVG_2026.pdf", "uvg", 3),
                            file_("NBU.pdf", "nbu", 2), file_("KTG.pdf", "ktg", 2),
                            file_("FAK.pdf", "fak", 2)]),
            self.BVG: page([file_("Plan 2026 BVG.xlsx", "bvg", 5)]),
            GRAPH + "/drives/%s/items/bvg/content" % DRIVE: b"BVG!!",
            GRAPH + "/drives/%s/items/uvg/content" % DRIVE: b"UVG",
            GRAPH + "/drives/%s/items/nbu/content" % DRIVE: b"NB",
            GRAPH + "/drives/%s/items/ktg/content" % DRIVE: b"KT",
            GRAPH + "/drives/%s/items/fak/content" % DRIVE: b"FA",
        }

    def tearDown(self):
        shutil.rmtree(self.out)

    def graph(self, routes=None):
        fake = FakeGraph(routes or self.routes)
        return fake, Graph(TENANT, CLIENT, DRIVE, self.cert_pem, self.key_pem, transport=fake)

    def test_saves_each_sheet_under_its_folder_with_mode_600(self):
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, "ins", self.out, out)
        with open(os.path.join(self.out, "BVG", "Plan 2026 BVG.xlsx"), "rb") as f:
            self.assertEqual(f.read(), b"BVG!!")
        with open(os.path.join(self.out, "UVG_2026.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"UVG")
        for root, dirs, files in os.walk(self.out):
            for name in files:
                mode = stat.S_IMODE(os.stat(os.path.join(root, name)).st_mode)
                self.assertEqual(mode, 0o600, name)
        self.assertIn("5 sheets saved under", out.getvalue())

    def test_nothing_is_downloaded_when_a_sheet_has_no_file(self):
        self.routes[self.INS] = page([folder("BVG", "f-bvg"), file_("UVG.pdf", "uvg", 3),
                                      file_("NBU.pdf", "nbu", 2), file_("KTG.pdf", "ktg", 2)])
        fake, g = self.graph()
        with self.assertRaises(SharePointError) as ctx:
            sp_read.fetch(g, "ins", self.out, io.StringIO())
        self.assertIn("FAK: 0 files match", str(ctx.exception))
        self.assertFalse([c for c in fake.calls if c[1].endswith("/content")])
        self.assertEqual(os.listdir(self.out), [])

    def test_nothing_is_guessed_when_a_sheet_matches_twice(self):
        self.routes[self.INS] = page([folder("BVG", "f-bvg"), file_("UVG.pdf", "uvg", 3),
                                      file_("UVG old.pdf", "uvg2", 3), file_("NBU.pdf", "nbu", 2),
                                      file_("KTG.pdf", "ktg", 2), file_("FAK.pdf", "fak", 2)])
        fake, g = self.graph()
        with self.assertRaises(SharePointError) as ctx:
            sp_read.fetch(g, "ins", self.out, io.StringIO())
        self.assertIn("UVG: 2 files match", str(ctx.exception))
        self.assertFalse([c for c in fake.calls if c[1].endswith("/content")])

    def test_a_size_that_differs_from_the_listing_is_an_error(self):
        self.routes[GRAPH + "/drives/%s/items/ktg/content" % DRIVE] = b"KTG-LONGER"
        fake, g = self.graph()
        with self.assertRaises(SharePointError):
            sp_read.fetch(g, "ins", self.out, io.StringIO())

    def test_names_that_could_leave_the_output_folder_are_refused(self):
        for folder_part, name in [((".." ,), "x.pdf"), ((), ".."), (("a/b",), "x.pdf"), ((), "a/b.pdf")]:
            with self.assertRaises(SharePointError):
                sp_read.target(self.out, folder_part, name)


class RefusalTest(Keys):
    def test_refusals_name_the_status_and_code_but_not_the_url_or_message(self):
        body = json.dumps({"error": {"code": "accessDenied", "message": "drive-x item ins not shared"}}).encode()
        err = urllib.error.HTTPError(GRAPH + "/drives/drive-x/items/ins", 403, "Forbidden", {}, io.BytesIO(body))
        with mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=err):
            with self.assertRaises(SharePointError) as ctx:
                sp_read.http("GET", GRAPH + "/drives/drive-x/items/ins", {}, None)
        text = str(ctx.exception)
        self.assertEqual(text, "GET refused with HTTP 403 (accessDenied)")
        self.assertNotIn("drive-x", text)


class RedirectTest(unittest.TestCase):
    def redirect(self, location):
        return urllib.error.HTTPError(GRAPH + "/x", 302, "Found", {"Location": location}, io.BytesIO(b""))

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def test_a_download_redirect_is_followed_without_the_bearer_token(self):
        seen = []

        def fake_open(opener, req, timeout=None):
            seen.append((req.full_url, dict(req.header_items())))
            if len(seen) == 1:
                raise self.redirect("https://files.example/blob")
            return self.Resp(b"body")

        with mock.patch.object(urllib.request.OpenerDirector, "open", fake_open):
            body = sp_read.http("GET", GRAPH + "/x", {"Authorization": "Bearer t"}, None)
        self.assertEqual(body, b"body")
        self.assertEqual(seen[1][0], "https://files.example/blob")
        self.assertNotIn("Authorization", seen[1][1])

    def test_a_redirect_to_plain_http_is_refused(self):
        with mock.patch.object(urllib.request.OpenerDirector, "open",
                               side_effect=self.redirect("http://files.example/blob")):
            with self.assertRaises(SharePointError):
                sp_read.http("GET", GRAPH + "/x", {"Authorization": "Bearer t"}, None)

    def test_the_opener_does_not_follow_redirects_itself(self):
        self.assertIsNone(sp_read.NoRedirect().redirect_request(None, None, 302, "", {}, "https://x"))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-cfg-")
        self.path = os.path.join(self.dir, "sharepoint.json")
        with open(self.path, "w") as f:
            json.dump({"tenant_id": "t"}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_a_config_others_can_read_is_refused(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(SharePointError):
            sp_read.load_config(self.path)

    def test_an_owner_only_config_loads(self):
        os.chmod(self.path, 0o600)
        self.assertEqual(sp_read.load_config(self.path), {"tenant_id": "t"})


if __name__ == "__main__":
    unittest.main()

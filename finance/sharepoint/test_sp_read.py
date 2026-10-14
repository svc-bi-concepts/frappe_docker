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
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(filename):
    """Run the launcher's python body as a module. The file is bash (av runs a
    Blessed Script as /dev/fd/N, and the mini's Python 3.9 runs such a script
    empty, IT 2026-10-14); the python travels on stdin between the heredoc
    markers, so the tests take it from between the markers."""
    name = filename[: -len(".py")] if filename.endswith(".py") else filename
    path = os.path.join(HERE, filename)
    with open(path) as f:
        lines = f.read().split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("exec ") and "<<" in line)
    tag = lines[start].split("<<", 1)[1].strip().strip("'")
    end = lines.index(tag, start + 1)
    module = sys.modules[name] = types.ModuleType(name)
    module.__file__ = path
    exec(compile("\n".join(lines[start + 1:end]) + "\n", path, "exec"), module.__dict__)
    return module


sp_read = _load("sp_read.py")
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
        thumbprint = hashlib.sha1(self.cert_der).hexdigest()
        return fake, Graph(TENANT, CLIENT, thumbprint, DRIVE, self.key_pem, transport=fake, now=now)

    def test_assertion_is_signed_by_the_certificates_key(self):
        fake, g = self.graph({})
        header, claims, sig = g.client_assertion().split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {
            "alg": "RS256", "typ": "JWT",
            "x5t": sp_read.b64url(hashlib.sha1(self.cert_der).digest())})
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
    FIN = GRAPH + "/drives/%s/items/fin/children" % DRIVE
    BVG = GRAPH + "/drives/%s/items/f-bvg/children" % DRIVE
    ROOTS = [("Insurance", "ins"), ("Finance", "fin")]

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="sp-out-")
        self.routes = {
            TOKEN_URL: json.dumps({"access_token": "fake-token"}).encode(),
            self.INS: page([folder("BVG", "f-bvg"), file_("UVG_2026.pdf", "uvg", 3),
                            file_("NBU.pdf", "nbu", 2), file_("KTG Vertrag 2026.pdf", "ktg", 2),
                            file_("KTG Antrag 2026.pdf", "ktg-app", 2), file_("FAK.pdf", "fak", 2)]),
            self.FIN: page([]),
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
        thumbprint = hashlib.sha1(self.cert_der).hexdigest()
        return fake, Graph(TENANT, CLIENT, thumbprint, DRIVE, self.key_pem, transport=fake)

    def test_saves_each_sheet_under_its_root_and_folder_with_mode_600(self):
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        with open(os.path.join(self.out, "Insurance", "BVG", "Plan 2026 BVG.xlsx"), "rb") as f:
            self.assertEqual(f.read(), b"BVG!!")
        with open(os.path.join(self.out, "Insurance", "UVG_2026.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"UVG")
        for root, dirs, files in os.walk(self.out):
            for name in files:
                mode = stat.S_IMODE(os.stat(os.path.join(root, name)).st_mode)
                self.assertEqual(mode, 0o600, name)
        self.assertIn("5 of 5 sheets saved under", out.getvalue())
        self.assertNotIn("MISSING", out.getvalue())

    def test_a_missing_sheet_is_reported_and_the_others_are_still_saved(self):
        self.routes[self.INS] = page([folder("BVG", "f-bvg"), file_("UVG.pdf", "uvg", 3),
                                      file_("NBU.pdf", "nbu", 2), file_("KTG Vertrag.pdf", "ktg", 2)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("MISSING FAK  0 files match, want exactly 1 (none)", out.getvalue())
        self.assertIn("4 of 5 sheets saved under", out.getvalue())
        downloaded = sorted(c[1] for c in fake.calls if c[1].endswith("/content"))
        self.assertFalse([u for u in downloaded if "/fak/" in u])
        self.assertEqual(len(downloaded), 4)

    def test_a_sheet_that_matches_twice_is_reported_and_not_guessed(self):
        self.routes[self.INS] = page([folder("BVG", "f-bvg"), file_("UVG.pdf", "uvg", 3),
                                      file_("UVG old.pdf", "uvg2", 3), file_("NBU.pdf", "nbu", 2),
                                      file_("KTG Vertrag.pdf", "ktg", 2), file_("FAK.pdf", "fak", 2)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("MISSING UVG  2 files match", out.getvalue())
        downloaded = [c[1] for c in fake.calls if c[1].endswith("/content")]
        self.assertFalse([u for u in downloaded if "/uvg" in u])

    def test_ktg_takes_the_contract_and_never_the_application_or_refusal(self):
        self.routes[self.INS] = page([folder("BVG", "f-bvg"), file_("UVG.pdf", "uvg", 3),
                                      file_("NBU.pdf", "nbu", 2), file_("KTG Antrag.pdf", "ktg-app", 2),
                                      file_("KTG Ablehnung.pdf", "ktg-no", 2), file_("KTG Vertrag.pdf", "ktg", 2),
                                      file_("FAK.pdf", "fak", 2)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("Insurance/KTG Vertrag.pdf", out.getvalue())
        downloaded = [c[1] for c in fake.calls if c[1].endswith("/content")]
        self.assertIn(GRAPH + "/drives/%s/items/ktg/content" % DRIVE, downloaded)
        self.assertFalse([u for u in downloaded if "ktg-app" in u or "ktg-no" in u])

    def test_two_ktg_contracts_are_reported_as_ambiguous(self):
        self.routes[self.INS] = page([folder("BVG", "f-bvg"), file_("UVG.pdf", "uvg", 3),
                                      file_("NBU.pdf", "nbu", 2), file_("KTG Vertrag 2025.pdf", "ktg-25", 2),
                                      file_("KTG Vertrag 2026.pdf", "ktg", 2), file_("FAK.pdf", "fak", 2)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("MISSING KTG  2 files match, want exactly 1", out.getvalue())
        downloaded = [c[1] for c in fake.calls if c[1].endswith("/content")]
        self.assertFalse([u for u in downloaded if "/ktg" in u])

    def test_a_token_glued_to_the_insurers_name_counts(self):
        # The contract's real name glues the insurer to the token with no separator and the
        # folder names the topic; invented names in that shape, since the word-edge matcher
        # alone found nothing there.
        self.routes[self.INS] = page([folder("SickPay", "f-sick"),
                                      file_("UVG.pdf", "uvg", 3), file_("NBU.pdf", "nbu", 2),
                                      file_("FAK.pdf", "fak", 2)])
        self.routes[GRAPH + "/drives/%s/items/f-sick/children" % DRIVE] = page([
            file_("20260401_VersichererKTG_Vertrag.pdf", "ktg", 2),
            file_("20260301_VersichererKTG_Antrag.pdf", "ktg-app", 2)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("Insurance/SickPay/20260401_VersichererKTG_Vertrag.pdf", out.getvalue())
        self.assertIn("MISSING BVG", out.getvalue())
        downloaded = [c[1] for c in fake.calls if c[1].endswith("/content")]
        self.assertIn(GRAPH + "/drives/%s/items/ktg/content" % DRIVE, downloaded)
        self.assertFalse([u for u in downloaded if "ktg-app" in u])

    def test_a_token_only_in_the_folder_name_counts(self):
        self.routes[self.INS] = page([folder("SickPay KTG", "f-sick"),
                                      file_("UVG.pdf", "uvg", 3), file_("NBU.pdf", "nbu", 2),
                                      file_("FAK.pdf", "fak", 2)])
        self.routes[GRAPH + "/drives/%s/items/f-sick/children" % DRIVE] = page([
            file_("Vertrag 2026.pdf", "ktg-ver", 2),
            file_("Antrag 2026.pdf", "ktg-app", 2)])
        self.routes[GRAPH + "/drives/%s/items/ktg-ver/content" % DRIVE] = b"KT"
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("Insurance/SickPay KTG/Vertrag 2026.pdf", out.getvalue())
        downloaded = [c[1] for c in fake.calls if c[1].endswith("/content")]
        self.assertFalse([u for u in downloaded if "ktg-app" in u])

    def test_a_word_with_a_letter_right_after_the_token_is_no_match(self):
        # 'Faktura' carries 'fak' but is not the FAK sheet: a letter directly after the token
        # keeps it out, so a lone Faktura leaves FAK missing instead of fetching an invoice.
        self.routes[self.INS] = page([file_("Faktura 2026.pdf", "inv", 2),
                                      file_("UVG.pdf", "uvg", 3), file_("NBU.pdf", "nbu", 2),
                                      file_("KTG Vertrag.pdf", "ktg", 2)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        self.assertIn("MISSING FAK  0 files match, want exactly 1 (none)", out.getvalue())
        downloaded = [c[1] for c in fake.calls if c[1].endswith("/content")]
        self.assertFalse([u for u in downloaded if "/inv/" in u])

    def test_a_sheet_found_only_in_finance_is_saved_under_finance(self):
        self.routes[self.INS] = page([file_("UVG.pdf", "uvg", 3), file_("NBU.pdf", "nbu", 2),
                                      file_("KTG Vertrag.pdf", "ktg", 2), file_("FAK.pdf", "fak", 2)])
        self.routes[self.FIN] = page([folder("Sozial", "f-soz")])
        self.routes[GRAPH + "/drives/%s/items/f-soz/children" % DRIVE] = page([file_("BVG 2026.pdf", "bvg", 5)])
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch(g, self.ROOTS, self.out, out)
        with open(os.path.join(self.out, "Finance", "Sozial", "BVG 2026.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"BVG!!")
        self.assertIn("5 of 5 sheets saved under", out.getvalue())

    def test_a_size_that_differs_from_the_listing_is_an_error(self):
        self.routes[GRAPH + "/drives/%s/items/ktg/content" % DRIVE] = b"KTG-LONGER"
        fake, g = self.graph()
        with self.assertRaises(SharePointError):
            sp_read.fetch(g, self.ROOTS, self.out, io.StringIO())

    def test_names_that_could_leave_the_output_folder_are_refused(self):
        for folder_part, name in [((".." ,), "x.pdf"), ((), ".."), (("a/b",), "x.pdf"), ((), "a/b.pdf")]:
            with self.assertRaises(SharePointError):
                sp_read.target(self.out, folder_part, name)


class FetchPathsTest(Keys):
    FIN = GRAPH + "/drives/%s/items/fin/children" % DRIVE
    ROOTS = [("Finance", "fin")]

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="sp-out-")
        self.routes = {
            TOKEN_URL: json.dumps({"access_token": "fake-token"}).encode(),
            self.FIN: page([folder("Acc", "f-acc"), file_("Root.pdf", "root", 2)]),
            GRAPH + "/drives/%s/items/f-acc/children" % DRIVE: page([
                file_("Beitrag 2026.pdf", "beitrag", 5), file_("Other.pdf", "other", 3)]),
            GRAPH + "/drives/%s/items/beitrag/content" % DRIVE: b"BEIT!",
            GRAPH + "/drives/%s/items/root/content" % DRIVE: b"RT",
            GRAPH + "/drives/%s/items/other/content" % DRIVE: b"OTH",
        }

    def tearDown(self):
        shutil.rmtree(self.out)

    def graph(self):
        fake = FakeGraph(self.routes)
        thumbprint = hashlib.sha1(self.cert_der).hexdigest()
        return fake, Graph(TENANT, CLIENT, thumbprint, DRIVE, self.key_pem, transport=fake)

    def test_saves_exactly_the_listed_paths_under_their_root_with_mode_600(self):
        fake, g = self.graph()
        out = io.StringIO()
        sp_read.fetch_paths(g, self.ROOTS, ["Finance/Acc/Beitrag 2026.pdf", "Finance/Root.pdf"], self.out, out)
        with open(os.path.join(self.out, "Finance", "Acc", "Beitrag 2026.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"BEIT!")
        self.assertFalse(os.path.exists(os.path.join(self.out, "Finance", "Acc", "Other.pdf")))
        for root, dirs, files in os.walk(self.out):
            for name in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, name)).st_mode), 0o600, name)
        self.assertIn("2 of 2 paths saved under", out.getvalue())

    def test_one_path_that_is_not_under_the_root_downloads_nothing(self):
        fake, g = self.graph()
        with self.assertRaises(SharePointError):
            sp_read.fetch_paths(g, self.ROOTS, ["Finance/Root.pdf", "Finance/Acc/Missing.pdf"], self.out, io.StringIO())
        self.assertFalse([c for c in fake.calls if c[1].endswith("/content")])
        self.assertFalse(os.listdir(self.out))

    def test_a_path_that_could_leave_the_output_folder_is_refused_before_any_download(self):
        self.routes[self.FIN] = page([file_("..", "dots", 2)])
        fake, g = self.graph()
        with self.assertRaises(SharePointError):
            sp_read.fetch_paths(g, self.ROOTS, ["Finance/.."], self.out, io.StringIO())
        self.assertFalse([c for c in fake.calls if c[1].endswith("/content")])

    def test_the_list_file_skips_blank_and_comment_lines(self):
        path = os.path.join(self.out, "paths.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# a comment\n\nFinance/Root.pdf\n  Finance/Acc/Beitrag 2026.pdf  \n")
        self.assertEqual(sp_read.read_paths(path), ["Finance/Root.pdf", "Finance/Acc/Beitrag 2026.pdf"])


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


class ErrorPathTest(Keys):
    def test_a_thumbprint_that_is_not_sha1_hex_is_refused(self):
        for bad in ("zz", "ab" * 19, None):
            with self.assertRaises(SharePointError):
                Graph(TENANT, CLIENT, bad, DRIVE, self.key_pem)

    def test_a_key_openssl_rejects_fails_without_echoing_it(self):
        bad = "-----BEGIN PRIVATE KEY-----\nnot-a-key-secret\n-----END PRIVATE KEY-----\n"
        g = Graph(TENANT, CLIENT, "ab" * 20, DRIVE, bad)
        with self.assertRaises(SharePointError) as ctx:
            g.client_assertion()
        self.assertNotIn("not-a-key-secret", str(ctx.exception))

    def test_main_without_the_vault_key_says_so_and_sends_nothing(self):
        cfg = {"tenant_id": "t", "client_id": "c", "thumbprint": "ab" * 20,
               "drive_id": "d", "folders": {"Insurance": "i"}}
        err = io.StringIO()
        with mock.patch.object(sp_read, "load_config", lambda: cfg), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sp_read, "OPENER") as opener, \
                mock.patch("sys.stderr", err):
            code = sp_read.main(["sp_read.py", "fetch"])
        self.assertEqual(code, 1)
        self.assertIn("FINANCE_SP_CERT_KEY", err.getvalue())
        opener.open.assert_not_called()

    def test_main_reads_the_key_from_finance_sp_cert_key_only(self):
        cfg = {"tenant_id": "t", "client_id": "c", "thumbprint": "ab" * 20,
               "drive_id": "d", "folders": {"Insurance": "i"}}
        old = "FINANCE_SP_CERT_KEY".removeprefix("FINANCE_")  # the clashing name, kept out of grep
        for env, expect_code in (({"FINANCE_SP_CERT_KEY": "invented-key"}, 0),
                                 ({old: "invented-key"}, 1)):
            with self.subTest(env=list(env)):
                seen = []
                with mock.patch.object(sp_read, "load_config", lambda: cfg), \
                        mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(sp_read, "Graph", lambda *a: seen.append(a[-1])), \
                        mock.patch.object(sp_read, "list_folder"), \
                        mock.patch("sys.stderr", io.StringIO()):
                    code = sp_read.main(["sp_read.py", "list", "Insurance"])
                self.assertEqual(code, expect_code)
                self.assertEqual(seen, ["invented-key"] if expect_code == 0 else [])


class HardeningTest(unittest.TestCase):
    """The file is blessed by hash, so what it runs with is pinned here: the shebang and the openssl path."""

    def test_the_first_line_is_the_av_inject_shebang_for_bash(self):
        path = os.path.join(os.path.dirname(os.path.abspath(sp_read.__file__)), "sp_read.py")
        with open(path) as f:
            first = f.readline().rstrip("\n")
        self.assertEqual(first, "#!/usr/local/bin/av inject +FINANCE_SP_CERT_KEY /bin/bash")

    def test_the_signing_call_runs_openssl_by_absolute_path(self):
        done = mock.Mock(returncode=0, stdout=b"sig")
        with mock.patch.object(sp_read.subprocess, "run", return_value=done) as run:
            self.assertEqual(sp_read.sign_rs256(b"data", "key"), b"sig")
        self.assertEqual(run.call_args[0][0][0], "/usr/bin/openssl")


if __name__ == "__main__":
    unittest.main()

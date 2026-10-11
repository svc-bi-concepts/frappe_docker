"""The SharePoint reader's tests, on a fake broker and a fake Graph: no network, no real token or ids.

Standard library only.
"""

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import sp_read
from sp_read import GRAPH, Graph, SharePointError

DRIVE = "drive-x"
BROKER = "http://127.0.0.1:18898/token/finance"
BEARER = "bearer-x"


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


def broker_answer(token="graph-token-x"):
    return json.dumps({"access_token": token, "expires_in": 3600}).encode()


class TokenFileCase(unittest.TestCase):
    """A temp dir holding the broker's bearer token file, as IT's broker keeps it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-tok-")
        self.token_file = os.path.join(self.dir, "graph-token")
        with open(self.token_file, "w") as f:
            f.write(BEARER + "\n")

    def tearDown(self):
        shutil.rmtree(self.dir)


class BrokerTest(TokenFileCase):
    def graph(self, routes=None, token_file=None):
        table = {BROKER: broker_answer()}
        table.update(routes or {})
        fake = FakeGraph(table)
        return fake, Graph(DRIVE, broker=BROKER, token_file=token_file or self.token_file, transport=fake)

    def test_the_token_comes_from_the_broker_with_its_bearer(self):
        fake, g = self.graph()
        self.assertEqual(g.access_token(), "graph-token-x")
        (method, url, headers, data), = fake.calls
        self.assertEqual((method, url, data), ("GET", BROKER, None))
        self.assertEqual(headers, {"Authorization": "Bearer " + BEARER})

    def test_the_token_is_fetched_once_per_run(self):
        fake, g = self.graph()
        g.access_token()
        g.access_token()
        self.assertEqual(len(fake.calls), 1)

    def test_the_bearer_file_is_read_when_the_token_is_needed(self):
        fake, g = self.graph()
        with open(self.token_file, "w") as f:
            f.write("bearer-second\n")
        g.access_token()
        self.assertEqual(fake.calls[0][2], {"Authorization": "Bearer bearer-second"})

    def test_a_missing_bearer_file_is_an_error_that_names_no_path(self):
        fake, g = self.graph(token_file=os.path.join(self.dir, "missing"))
        with self.assertRaises(SharePointError) as ctx:
            g.access_token()
        self.assertEqual(str(ctx.exception), "cannot read the broker's token file")
        self.assertEqual(fake.calls, [])

    def test_a_broker_answer_without_a_token_is_an_error(self):
        fake, g = self.graph({BROKER: b'{"expires_in": 5}'})
        with self.assertRaises(SharePointError):
            g.access_token()

    def test_a_broker_answer_that_is_not_json_is_an_error(self):
        fake, g = self.graph({BROKER: b"<html>"})
        with self.assertRaises(SharePointError):
            g.access_token()

    def test_the_broker_must_be_loopback_http(self):
        for broker in ("https://127.0.0.1:18898/token/finance",
                       "http://broker.example/token/finance",
                       "http://192.168.1.5:18898/token/finance"):
            with self.assertRaises(SharePointError):
                Graph(DRIVE, broker=broker, token_file=self.token_file, transport=FakeGraph({}))

    def test_the_bearer_goes_only_to_the_broker_url_by_get(self):
        fake, g = self.graph()
        with self.assertRaises(SharePointError):
            g._send("GET", "http://127.0.0.1:18898/token/other", {"Authorization": "Bearer x"})
        with self.assertRaises(SharePointError):
            g._send("POST", BROKER, {"Authorization": "Bearer x"}, b"x")
        self.assertEqual(fake.calls, [])


class GraphTest(TokenFileCase):
    def graph(self, routes):
        fake = FakeGraph(dict({BROKER: broker_answer()}, **routes))
        return fake, Graph(DRIVE, broker=BROKER, token_file=self.token_file, transport=fake)

    def test_children_follow_paging(self):
        first = GRAPH + "/drives/%s/items/root/children?page=2" % DRIVE
        fake, g = self.graph({
            GRAPH + "/drives/%s/items/ins/children" % DRIVE:
                page([file_("a.pdf", "1", 3)], next_link=first),
            first: page([file_("b.pdf", "2", 4)]),
        })
        self.assertEqual([i["name"] for i in g.children("ins")], ["a.pdf", "b.pdf"])

    def test_graph_calls_carry_the_broker_token(self):
        fake, g = self.graph({GRAPH + "/drives/%s/items/ins/children" % DRIVE: page([])})
        g.children("ins")
        graph_call = [c for c in fake.calls if c[1].startswith(GRAPH)][0]
        self.assertEqual(graph_call[2], {"Authorization": "Bearer graph-token-x"})

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

    def test_only_gets_to_graph_and_the_broker_leave(self):
        fake, g = self.graph({
            GRAPH + "/drives/%s/items/ins/children" % DRIVE: page([file_("UVG.pdf", "1", 2)]),
            GRAPH + "/drives/%s/items/1/content" % DRIVE: b"..",
        })
        self.assertEqual(g.download("1"), b"..")
        list(g.walk("ins"))
        for method, url, _, _ in fake.calls:
            self.assertTrue((method == "GET" and url.startswith(GRAPH + "/")) or
                            (method == "GET" and url == BROKER), (method, url))
        for method in ("PUT", "PATCH", "DELETE", "POST"):
            with self.assertRaises(SharePointError):
                g._send(method, GRAPH + "/drives/%s/items/1" % DRIVE, {}, b"x")
        with self.assertRaises(SharePointError):
            g._send("GET", "https://login.example/token", {})
        with self.assertRaises(SharePointError):
            g._send("POST", GRAPH + "/drives/%s/items/1/children" % DRIVE, {}, b"x")


class FetchTest(TokenFileCase):
    INS = GRAPH + "/drives/%s/items/ins/children" % DRIVE
    BVG = GRAPH + "/drives/%s/items/f-bvg/children" % DRIVE

    def setUp(self):
        super().setUp()
        self.out = tempfile.mkdtemp(prefix="sp-out-")
        self.routes = {
            BROKER: broker_answer(),
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
        super().tearDown()

    def graph(self, routes=None):
        fake = FakeGraph(routes or self.routes)
        return fake, Graph(DRIVE, broker=BROKER, token_file=self.token_file, transport=fake)

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


class RefusalTest(unittest.TestCase):
    def test_refusals_name_the_status_and_code_but_not_the_url_or_message(self):
        body = json.dumps({"error": {"code": "accessDenied", "message": "drive-x item ins not shared"}}).encode()
        err = urllib.error.HTTPError(GRAPH + "/drives/drive-x/items/ins", 403, "Forbidden", {}, io.BytesIO(body))
        with mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=err):
            with self.assertRaises(SharePointError) as ctx:
                sp_read.http("GET", GRAPH + "/drives/drive-x/items/ins", {}, None)
        text = str(ctx.exception)
        self.assertEqual(text, "GET refused with HTTP 403 (accessDenied)")
        self.assertNotIn("drive-x", text)

    def test_a_broker_refusal_names_no_token(self):
        body = json.dumps({"error": "unauthorized", "detail": "bearer-x is not valid"}).encode()
        err = urllib.error.HTTPError(BROKER, 401, "Unauthorized", {}, io.BytesIO(body))
        with mock.patch.object(urllib.request.OpenerDirector, "open", side_effect=err):
            with self.assertRaises(SharePointError) as ctx:
                sp_read.http("GET", BROKER, {"Authorization": "Bearer bearer-x"}, None)
        self.assertEqual(str(ctx.exception), "GET refused with HTTP 401 (unauthorized)")


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
            json.dump({"drive_id": "d"}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_a_config_others_can_read_is_refused(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(SharePointError):
            sp_read.load_config(self.path)

    def test_an_owner_only_config_loads(self):
        os.chmod(self.path, 0o600)
        self.assertEqual(sp_read.load_config(self.path), {"drive_id": "d"})


if __name__ == "__main__":
    unittest.main()

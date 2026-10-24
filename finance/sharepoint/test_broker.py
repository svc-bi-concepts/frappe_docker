"""Offline broker tests; Graph is faked and all ids/names are invented."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import broker


class Reply:
    def __init__(self, status=200, value=None, body=b""):
        self.status = status
        self.value = value or {}
        self.body = body

    def json(self):
        return self.value


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.upload_urls = set()
        self.next_response = Reply(200, {"name": "report.csv", "size": 4})
        self.fail_chunk = False
        self.existing = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and kwargs.get("missing_ok"):
            return (
                Reply(value={"name": "report.csv", "size": 4})
                if self.existing
                else None
            )
        if method == "POST":
            self.upload_urls.add("https://upload.invalid/session")
            return Reply(value={"uploadUrl": "https://upload.invalid/session"})
        if url in self.upload_urls:
            if method == "DELETE":
                return Reply(204)
            if self.fail_chunk:
                raise broker.Fail(502, "chunk refused")
            return Reply(
                201, {"name": "report.csv", "size": len(kwargs.get("data", b""))}
            )
        return self.next_response


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        self.inbox = os.path.join(self.tmp.name, "inbox")
        os.makedirs(self.out)
        os.makedirs(self.inbox)
        self.old_paths = broker.OUT_DIR, broker.INBOX_DIR
        broker.OUT_DIR, broker.INBOX_DIR = self.out, self.inbox
        self.client = FakeGraph()
        self.old_client = broker.GRAPH_CLIENT
        broker.GRAPH_CLIENT = self.client
        self.old_areas = broker.AREAS
        broker.AREAS = {
            "out": {
                "drive_id": "drive-out",
                "item_id": "item-out",
                "path": "/Finance/ws_finance",
            },
            "sample": {
                "drive_id": "drive-in",
                "item_id": "item-in",
                "path": "/Finance/sample",
            },
        }

    def tearDown(self):
        broker.OUT_DIR, broker.INBOX_DIR = self.old_paths
        broker.GRAPH_CLIENT, broker.AREAS = self.old_client, self.old_areas
        self.tmp.cleanup()

    def local_file(self, name="report.csv", data=b"data"):
        path = os.path.join(self.out, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_src_outside_out_and_symlink_are_refused(self):
        external = os.path.join(self.tmp.name, "outside")
        with open(external, "wb") as f:
            f.write(b"x")
        with self.assertRaises(broker.Fail) as caught:
            broker.local_src(external)
        self.assertEqual(caught.exception.code, 403)
        link = os.path.join(self.out, "escape")
        os.symlink(external, link)
        with self.assertRaises(broker.Fail) as caught:
            broker.local_src(link)
        self.assertEqual(caught.exception.code, 403)

    def test_bad_remote_path_refused(self):
        with self.assertRaises(broker.Fail) as caught:
            broker.clean("folder/../secret")
        self.assertEqual(caught.exception.code, 400)

    def test_graph_refuses_write_to_read_area(self):
        client = broker.Graph(
            {
                "tenant_id": "t",
                "client_id": "c",
                "thumbprint": "00" * 20,
                "out": {"drive_id": "drive-out", "item_id": "item-out"},
                "folders": {},
            },
            "key",
            transport=lambda *args: Reply(),
        )
        client.token = "fake"
        with self.assertRaises(broker.Fail) as caught:
            client.request("PUT", broker.area_url("sample", "x"))
        self.assertEqual(caught.exception.code, 403)

    def test_reads_in_area_do_not_write(self):
        self.client.next_response = Reply(
            value={"value": [{"name": "draft.txt", "file": {}, "size": 4}]}
        )
        broker.do_ls({"area": "sample"})
        self.assertTrue(all(method == "GET" for method, _, _ in self.client.calls))
        self.client.calls.clear()

        def fake_graph(method, url, **kwargs):
            self.client.calls.append((method, url, kwargs))
            if url.endswith("/content"):
                return Reply(body=b"read only")
            return Reply(value={"name": "draft.txt", "file": {}, "size": 9})

        with mock.patch.object(broker, "graph", side_effect=fake_graph):
            broker.do_get_file({"area": "sample", "path": "draft.txt"})
        self.assertTrue(all(method == "GET" for method, _, _ in self.client.calls))

    def test_existing_file_requires_replace_and_replace_succeeds(self):
        path = self.local_file()
        self.client.existing = True
        self.client.next_response = Reply(value={"name": "report.csv", "size": 4})
        with mock.patch.object(broker, "graph", side_effect=self.client.request):
            with self.assertRaises(broker.Fail) as caught:
                broker.do_put_file({"src": path, "path": "report.csv"})
            self.assertEqual(caught.exception.code, 409)
            result = broker.do_put_file(
                {"src": path, "path": "report.csv", "replace": "true"}
            )
        self.assertEqual(result["size"], 4)
        writes = [
            (m, u) for m, u, _ in self.client.calls if m in ("PUT", "POST", "DELETE")
        ]
        self.assertTrue(all("/items/item-out" in url for _, url in writes))

    def test_large_file_uses_upload_session_and_cancels_failure(self):
        path = self.local_file(data=b"x" * (broker.SIMPLE_MAX_FILE + 1))
        self.client.next_response = Reply(404)
        with mock.patch.object(broker, "graph", side_effect=self.client.request):
            result = broker.do_put_file({"src": path, "path": "large.csv"})
        self.assertEqual(result["size"], broker.SIMPLE_MAX_FILE + 1)
        self.assertTrue(
            any(
                m == "POST" and "createUploadSession" in u
                for m, u, _ in self.client.calls
            )
        )

        self.client.calls.clear()
        self.client.existing = False
        self.client.fail_chunk = True
        with mock.patch.object(broker, "graph", side_effect=self.client.request):
            with self.assertRaises(broker.Fail):
                broker.do_put_file({"src": path, "path": "large.csv"})
        self.assertTrue(
            any(
                m == "DELETE" and "upload.invalid" in u for m, u, _ in self.client.calls
            )
        )

    def test_ooxml_size_rewrite_allowed_but_other_size_mismatch_fails(self):
        path = self.local_file("model.xlsx", b"zip")
        self.client.next_response = Reply(value={"name": "model.xlsx", "size": 4})
        with mock.patch.object(broker, "graph", side_effect=self.client.request):
            result = broker.do_put_file(
                {"src": path, "path": "model.xlsx", "replace": "true"}
            )
            self.assertTrue(result["sharepoint_modified"])
            with self.assertRaises(broker.Fail):
                broker.do_put_file(
                    {"src": path, "path": "model.csv", "replace": "true"}
                )

    def test_get_file_rejects_path_outside_inbox(self):
        root = os.path.join(self.inbox, "sample")
        os.symlink(self.tmp.name, root)
        with self.assertRaises(broker.Fail) as caught:
            broker.inbox_path("sample", "escape.txt")
        self.assertEqual(caught.exception.code, 403)

    def test_get_file_saves_content_with_digest(self):
        content = b"sample"

        def fake_graph(method, url, **kwargs):
            if url.endswith("/content"):
                return Reply(body=content)
            return Reply(value={"name": "sample.txt", "size": len(content), "file": {}})

        with mock.patch.object(broker, "graph", side_effect=fake_graph):
            result = broker.do_get_file({"area": "sample", "path": "folder/sample.txt"})
        self.assertTrue(
            result["saved"].startswith(os.path.realpath(self.inbox) + os.sep)
        )
        with open(result["saved"], "rb") as f:
            self.assertEqual(f.read(), content)
        self.assertEqual(
            result["sha256"], __import__("hashlib").sha256(content).hexdigest()
        )

    def test_unknown_area_refused(self):
        with self.assertRaises(broker.Fail) as caught:
            broker.area_url("unknown", "file")
        self.assertEqual(caught.exception.code, 400)

    def test_config_must_be_private(self):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w") as f:
            json.dump({"folders": {}, "out": {}}, f)
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ValueError, "mode 600"):
            broker.load_config(path)
        os.chmod(path, 0o400)
        with self.assertRaisesRegex(ValueError, "mode 600"):
            broker.load_config(path)

    def test_bearer_comparison_requires_exact_token(self):
        self.assertTrue(broker.authorized("Bearer secret", "secret"))
        self.assertFalse(broker.authorized("Bearer wrong", "secret"))
        self.assertFalse(broker.authorized("", "secret"))
        self.assertFalse(broker.authorized("Bearer secret", ""))

    def test_replacing_a_folder_is_refused(self):
        self.client.existing = True
        original = self.client.request

        def request(method, url, **kwargs):
            if method == "GET":
                return Reply(value={"folder": {}})
            return original(method, url, **kwargs)

        with mock.patch.object(broker, "graph", side_effect=request):
            with self.assertRaises(broker.Fail) as err:
                broker.do_put_file(
                    {"src": self.local_file(), "path": "folder", "replace": "true"}
                )
        self.assertEqual(err.exception.code, 409)
        self.assertFalse(self.client.calls)

    def test_get_file_unknown_area_fails_before_graph_call(self):
        with self.assertRaises(broker.Fail) as caught:
            broker.do_get_file({"area": "absent", "path": "one.txt"})
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.client.calls, [])


class GraphTest(unittest.TestCase):
    def client(self, transport):
        client = broker.Graph(
            {
                "tenant_id": "tenant-invented",
                "client_id": "client-invented",
                "thumbprint": "ab" * 20,
                "drive_id": "drive-invented",
                "folders": {"sample": "item-invented"},
                "out": {"drive_id": "drive-output", "item_id": "item-output"},
            },
            "invented-private-key",
            transport=transport,
        )
        return client

    def test_token_is_reused_then_refreshed_before_expiry(self):
        transport = mock.Mock(
            side_effect=[
                Reply(value={"access_token": "first", "expires_in": 3600}),
                Reply(value={"access_token": "second", "expires_in": 3600}),
            ]
        )
        client = self.client(transport)
        with mock.patch.object(
            client, "assertion", return_value="signed"
        ), mock.patch.object(
            broker.time, "monotonic", side_effect=[0, 100, 3541, 3541]
        ):
            self.assertEqual(client.access_token(), "first")
            self.assertEqual(client.access_token(), "first")
            self.assertEqual(client.access_token(), "second")
        self.assertEqual(transport.call_count, 2)

    def test_write_boundary_and_methods_are_enforced_before_transport(self):
        transport = mock.Mock()
        client = self.client(transport)
        output = broker.GRAPH + "/drives/drive-output/items/item-output"
        for method, url in (
            ("PUT", output + "-other:/file:/content"),
            ("POST", broker.GRAPH + "/drives/drive-invented/items/item-invented"),
            ("DELETE", output),
            ("PATCH", output),
            ("GET", "https://untrusted.invalid/file"),
        ):
            with self.subTest(method=method, url=url), self.assertRaises(
                broker.Fail
            ) as err:
                client.request(method, url)
            self.assertEqual(err.exception.code, 403)
        transport.assert_not_called()

    def test_reserved_or_invalid_area_names_are_refused_before_resolution(self):
        client = self.client(mock.Mock())
        for name in ("out", "../escape", "bad name"):
            config = dict(client.config, folders={name: "item-invented"})
            with self.assertRaises(ValueError):
                broker.resolve_areas(client, config)
        client.transport.assert_not_called()

    def test_graph_conflict_is_reported_as_conflict(self):
        client = self.client(lambda *args: Reply(409))
        with mock.patch.object(client, "access_token", return_value="fake"):
            with self.assertRaises(broker.Fail) as err:
                client.request(
                    "PUT",
                    broker.GRAPH
                    + "/drives/drive-output/items/item-output:/report:/content",
                )
        self.assertEqual(err.exception.code, 409)

    def test_download_redirect_strips_bearer_and_refuses_plain_http(self):
        class Download(io.BytesIO):
            status = 200
            headers = {}

        seen = []

        def open_request(opener, request, timeout=None):
            seen.append(request)
            if len(seen) == 1:
                raise broker.urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {"Location": "https://files.invalid/download"},
                    io.BytesIO(),
                )
            return Download(b"sample")

        with mock.patch.object(
            broker.urllib.request.OpenerDirector, "open", open_request
        ):
            response = broker.Graph._http(
                "GET",
                broker.GRAPH + "/file/content",
                {"Authorization": "Bearer invented"},
            )
        self.assertEqual(response.body, b"sample")
        self.assertFalse(seen[1].has_header("Authorization"))
        redirect = broker.urllib.error.HTTPError(
            broker.GRAPH + "/file/content",
            302,
            "Found",
            {"Location": "http://files.invalid/download"},
            io.BytesIO(),
        )
        with mock.patch.object(
            broker.urllib.request.OpenerDirector, "open", side_effect=redirect
        ):
            with self.assertRaises(broker.Fail):
                broker.Graph._http("GET", broker.GRAPH + "/file/content")

    def test_signing_failure_does_not_echo_key_or_openssl_stderr(self):
        secret = "invented-private-key"
        with mock.patch.object(
            broker.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stderr=secret.encode()),
        ):
            with self.assertRaises(RuntimeError) as err:
                broker.sign_rs256(b"data", secret)
        self.assertNotIn(secret, str(err.exception))

    def test_real_graph_layer_uploads_multiple_chunks_and_cancels_failure(self):
        with tempfile.TemporaryDirectory() as out:
            content = b"x" * (broker.CHUNK + 17)
            src = os.path.join(out, "report.bin")
            with open(src, "wb") as stream:
                stream.write(content)
            for fail in (False, True):
                calls = []

                def transport(method, url, headers, data):
                    calls.append((method, url, headers, data))
                    if method == "GET":
                        return Reply(404)
                    if method == "POST":
                        return Reply(
                            value={"uploadUrl": "https://upload.invalid/session"}
                        )
                    self.assertNotIn("Authorization", headers)
                    if method == "DELETE":
                        return Reply(204)
                    if fail:
                        return Reply(503)
                    if headers["Content-Range"].startswith("bytes 0-"):
                        return Reply(202)
                    return Reply(201, {"size": len(content), "name": "report.bin"})

                client = self.client(transport)
                areas = {"out": client.config["out"]}
                with mock.patch.object(broker, "OUT_DIR", out), mock.patch.object(
                    broker, "GRAPH_CLIENT", client
                ), mock.patch.object(broker, "AREAS", areas), mock.patch.object(
                    client, "access_token", return_value="fake"
                ):
                    if fail:
                        with self.assertRaises(broker.Fail):
                            broker.do_put_file({"src": src, "path": "report.bin"})
                        self.assertEqual(calls[-1][0], "DELETE")
                    else:
                        result = broker.do_put_file({"src": src, "path": "report.bin"})
                        chunks = [call for call in calls if call[0] == "PUT"]
                        self.assertEqual(
                            [len(c[3]) for c in chunks], [broker.CHUNK, 17]
                        )
                        self.assertEqual(b"".join(c[3] for c in chunks), content)
                        self.assertEqual(
                            chunks[1][2]["Content-Range"],
                            "bytes %d-%d/%d"
                            % (broker.CHUNK, len(content) - 1, len(content)),
                        )
                        self.assertEqual(
                            result["sha256"], hashlib.sha256(content).hexdigest()
                        )
                self.assertFalse(client.upload_urls)


class HandlerTest(unittest.TestCase):
    def call(
        self, body=b"{}", auth="Bearer invented-token", length=None, route="/ping"
    ):
        handler = object.__new__(broker.Handler)
        handler.headers = {
            "Authorization": auth,
            "Content-Length": str(len(body)) if length is None else length,
        }
        handler.path = route
        handler.rfile = io.BytesIO(body)
        handler.reply = mock.Mock()
        with mock.patch.object(broker, "TOKEN", "invented-token"):
            handler.do_POST()
        return handler.reply.call_args.args

    def test_bad_bearer_is_unauthorized_including_non_ascii(self):
        for auth in ("", "Bearer wrong", "Bearer é"):
            self.assertEqual(self.call(auth=auth)[0], 401)

    def test_invalid_bodies_and_lengths_are_bad_requests(self):
        for body in (b"[]", b"null", b"broken", b"\xff"):
            self.assertEqual(self.call(body=body)[0], 400)
        for length in ("-1", "invalid"):
            self.assertEqual(self.call(length=length)[0], 400)

    def test_exception_and_request_logging_do_not_expose_secrets(self):
        secret = "invented-private-key"
        with mock.patch.dict(
            broker.ROUTES, {"/failure": mock.Mock(side_effect=RuntimeError(secret))}
        ):
            code, result = self.call(route="/failure")
        self.assertEqual(code, 500)
        self.assertNotIn(secret, json.dumps(result))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            broker.Handler.log_message(None, "%s", secret)
        self.assertNotIn(secret, log.getvalue())


if __name__ == "__main__":
    unittest.main()

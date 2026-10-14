"""Offline tests for upload_bill.py: the one upload request, its guards, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

import launcher

os.environ["BEXIO_DIR"] = os.path.dirname(os.path.abspath(__file__))  # run this checkout's files, never the main checkout's
ub = launcher.load("upload_bill.py")  # the launcher is a bash file; the tests run its python body
from private_guard import setUpModule  # every test below runs against a temp private folder, never the real one

PDF = b"%PDF-1.4\n% invented test bill\n"
TOKEN = "tok-invented-not-real"


def _pdf(name="bill-a.pdf", content=PDF):
    folder = os.path.join(ub.PRIVATE, "bills")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _answer(body):
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def _run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ub.main(argv)
    return out.getvalue()


def setUp_token(case):
    patcher = mock.patch.dict(os.environ, {"BEXIO_TOKEN": TOKEN})
    patcher.start()
    case.addCleanup(patcher.stop)


class DryRunTest(unittest.TestCase):
    def setUp(self):
        setUp_token(self)

    def test_default_is_a_dry_run_that_sends_nothing(self):
        path = _pdf()
        with mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
            out = _run([path])
        urlopen.assert_not_called()
        self.assertIn("dry run: would upload bill-a.pdf", out)
        self.assertIn("nothing sent", out)


class UploadTest(unittest.TestCase):
    def setUp(self):
        setUp_token(self)

    def test_posts_the_pdf_once_to_the_file_endpoint(self):
        path = _pdf()
        with mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(b'[{"id": 4711, "uuid": "uuid-invented-1"}]')) as urlopen:
            out = _run(["--yes", path])
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, ub.client.BASE_URL + "/3.0/files")
        self.assertTrue(req.get_header("Content-type").startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="bill-a.pdf"', req.data)
        self.assertIn(PDF, req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertIn("bexio file id 4711 (uuid uuid-invented-1)", out)
        self.assertNotIn(TOKEN, out)

    def test_refused_file_stops_before_any_request(self):
        good = _pdf()
        with mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(["--yes", good, os.path.join(ub.PRIVATE, "bills", "missing.pdf")])
        urlopen.assert_not_called()

    def test_http_error_is_reported_with_status_and_no_token(self):
        path = _pdf()
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error": "invented"}'))
        with mock.patch.object(ub.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes", path])
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_answer_without_id_is_an_error(self):
        path = _pdf()
        for body in (b"{}", b"[]", b"[{}]"):
            with self.subTest(body=body), mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(body)):
                with self.assertRaises(SystemExit):
                    _run(["--yes", path])


class RequestGuardTest(unittest.TestCase):
    def test_only_the_upload_request_is_sent(self):
        refused = [
            ("GET", "/3.0/files"),
            ("PUT", "/3.0/files"),
            ("DELETE", "/3.0/files/file-invented-1"),
            ("POST", "/4.0/purchase/bills"),
            ("POST", "/3.0/files/file-invented-1/download"),
        ]
        for method, path in refused:
            with self.subTest(method=method, path=path), \
                    mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(ub.RefusedRequest):
                    ub._send(method, path, b"", "text/plain", TOKEN)
                urlopen.assert_not_called()


class FileGuardTest(unittest.TestCase):
    def test_refuses_a_file_that_is_not_a_pdf(self):
        path = _pdf("notes.txt", b"invented")
        with self.assertRaises(SystemExit):
            ub.check_pdf(path)

    def test_refuses_a_missing_file(self):
        with self.assertRaises(SystemExit):
            ub.check_pdf(os.path.join(ub.PRIVATE, "bills", "nothing-here.pdf"))

    def test_refuses_a_pdf_outside_the_private_folder(self):
        outside = tempfile.mkdtemp(prefix="bexio-upload-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        path = os.path.join(outside, "bill-b.pdf")
        with open(path, "wb") as f:
            f.write(PDF)
        with self.assertRaises(SystemExit):
            ub.check_pdf(path)

    def test_refuses_a_symlink_that_leads_out_of_the_private_folder(self):
        outside = tempfile.mkdtemp(prefix="bexio-upload-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "bill-c.pdf")
        with open(target, "wb") as f:
            f.write(PDF)
        link = os.path.join(ub.PRIVATE, "bills", "link.pdf")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
        self.addCleanup(os.remove, link)
        with self.assertRaises(SystemExit):
            ub.check_pdf(link)

    def test_accepts_a_pdf_under_the_private_folder(self):
        path = _pdf("bill-d.pdf")
        self.assertEqual(ub.check_pdf(path), os.path.realpath(path))


class TokenTest(unittest.TestCase):
    def test_refuses_without_the_token_even_for_a_dry_run(self):
        path = _pdf()
        with mock.patch.dict(os.environ, clear=False) as env, mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
            env.pop("BEXIO_TOKEN", None)
            for argv in ([path], ["--yes", path]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm:
                    _run(argv)
                self.assertIn("BEXIO_TOKEN", str(cm.exception))
        urlopen.assert_not_called()


class LauncherTest(unittest.TestCase):
    """The file is blessed by hash, so what it runs with is pinned here: the shebang, the mode, no varlock."""

    PATH = os.path.join(os.path.dirname(os.path.abspath(ub.__file__)), "upload_bill.py")
    SHEBANG = "#!/usr/local/bin/av inject +BEXIO_TOKEN /bin/bash"

    def test_the_first_line_is_the_av_inject_shebang_for_bash(self):
        with open(self.PATH) as f:
            first = f.readline().rstrip("\n")
        self.assertEqual(first, self.SHEBANG)

    def test_the_file_is_executable(self):
        self.assertTrue(os.access(self.PATH, os.X_OK), "chmod +x upload_bill.py")

    def test_no_varlock_is_left_in_the_file(self):
        with open(self.PATH) as f:
            self.assertNotIn("varlock", f.read().lower())


class ResultFileTest(unittest.TestCase):
    """av inject drops the child's stdout, so a real upload also lands in private/bexio-upload/last-result.txt."""

    def setUp(self):
        setUp_token(self)
        shutil.rmtree(os.path.join(ub.PRIVATE, "bexio-upload"), ignore_errors=True)

    def result(self):
        with open(os.path.join(ub.PRIVATE, "bexio-upload", "last-result.txt")) as f:
            return f.read()

    def test_a_real_upload_appends_the_result_line_with_ids_and_no_token(self):
        path = _pdf()
        with mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(b'[{"id": 4711, "uuid": "uuid-invented-1"}]')):
            _run(["--yes", path])
        content = self.result()
        self.assertIn("bill-a.pdf", content)
        self.assertIn("bexio file id 4711", content)
        self.assertIn("uuid-invented-1", content)
        self.assertNotIn(TOKEN, content)

    def test_two_uploads_append_two_lines(self):
        path = _pdf()
        with mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(b'[{"id": 4711, "uuid": "uuid-invented-1"}]')):
            _run(["--yes", path])
        with mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(b'[{"id": 4712, "uuid": "uuid-invented-2"}]')):
            _run(["--yes", path])
        lines = self.result().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("4712", lines[1])

    def test_a_dry_run_writes_no_result_file(self):
        path = _pdf()
        _run([path])
        self.assertFalse(os.path.exists(os.path.join(ub.PRIVATE, "bexio-upload", "last-result.txt")))


if __name__ == "__main__":
    unittest.main()

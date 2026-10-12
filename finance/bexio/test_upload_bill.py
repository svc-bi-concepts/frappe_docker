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

import upload_bill as ub
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


class DryRunTest(unittest.TestCase):
    def test_default_is_a_dry_run_that_sends_nothing(self):
        path = _pdf()
        with mock.patch.object(ub.client, "resolve_token") as resolve, \
                mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
            out = _run([path])
        resolve.assert_not_called()
        urlopen.assert_not_called()
        self.assertIn("dry run: would upload bill-a.pdf", out)
        self.assertIn("nothing sent", out)


class UploadTest(unittest.TestCase):
    def test_posts_the_pdf_once_to_the_file_endpoint(self):
        path = _pdf()
        with mock.patch.object(ub.client, "resolve_token", return_value=(TOKEN, "oauth-export")), \
                mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(b'{"id": "file-invented-1"}')) as urlopen:
            out = _run(["--yes", path])
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, ub.client.BASE_URL + "/3.0/files")
        self.assertTrue(req.get_header("Content-type").startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="bill-a.pdf"', req.data)
        self.assertIn(PDF, req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertIn("bexio file id file-invented-1", out)
        self.assertNotIn(TOKEN, out)

    def test_refused_file_stops_before_any_request(self):
        good = _pdf()
        with mock.patch.object(ub.client, "resolve_token", return_value=(TOKEN, "oauth-export")), \
                mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(["--yes", good, os.path.join(ub.PRIVATE, "bills", "missing.pdf")])
        urlopen.assert_not_called()

    def test_http_error_is_reported_with_status_and_no_token(self):
        path = _pdf()
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error": "invented"}'))
        with mock.patch.object(ub.client, "resolve_token", return_value=(TOKEN, "oauth-export")), \
                mock.patch.object(ub.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                _run(["--yes", path])
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_answer_without_id_is_an_error(self):
        path = _pdf()
        with mock.patch.object(ub.client, "resolve_token", return_value=(TOKEN, "oauth-export")), \
                mock.patch.object(ub.urllib.request, "urlopen", return_value=_answer(b"{}")):
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


class BrokerTest(unittest.TestCase):
    def test_refuses_to_run_through_the_broker(self):
        path = _pdf()
        with mock.patch.dict(os.environ, {"BEXIO_BROKER": "1"}), \
                mock.patch.object(ub.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit):
                _run(["--yes", path])
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

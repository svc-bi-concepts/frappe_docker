"""Synthetic tests for invoice_sweep; no mailbox, private data, or network."""

import datetime
import http.client
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import invoice_sweep as sweep


class Response:
    def __init__(self, data=b"%PDF-1.7 synthetic", headers=None):
        self.data, self.offset = data, 0
        self.headers = headers or {}

    def read(self, n):
        part = self.data[self.offset : self.offset + n]
        self.offset += len(part)
        return part


class Opener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.config = os.path.join(self.root, "sweep.json")
        with open(self.config, "w") as f:
            json.dump(
                {
                    "folders": ["inbox"],
                    "hosts": ["example.test"],
                    "forwarders": [],
                    "senders": [],
                    "ignore": [],
                },
                f,
            )
        os.chmod(self.config, 0o600)
        self.incoming = os.path.join(self.root, "incoming")
        self.state = os.path.join(self.root, "state.json")
        self.log = os.path.join(self.root, "log.tsv")

    def runner(self, rows=(), mails=None, list_error=False):
        mails = mails or {}
        calls = []

        def run(argv, **kw):
            args = argv[1:]
            calls.append(args)
            if args[0] == "list":
                if list_error:
                    return sweep.subprocess.CompletedProcess(argv, 1, "", "")
                return sweep.subprocess.CompletedProcess(
                    argv,
                    0,
                    "".join(
                        "\t".join([r[0], r[1], r[2], r[3], "", "preview"]) + "\n"
                        for r in rows
                    ),
                    "",
                )
            if args[0] == "read":
                value = mails.get(args[1])
                return sweep.subprocess.CompletedProcess(
                    argv, 0 if value is not None else 1, json.dumps(value or {}), ""
                )

        return run, calls

    def mail(
        self,
        sender="vendor@example.test",
        subject="Invoice",
        attachments=(),
        links=(),
        text="",
    ):
        return {
            "from": sender,
            "received": "2026-10-30T12:00:00Z",
            "subject": subject,
            "attachments": list(attachments),
            "links": list(links),
            "text": text,
        }

    def test_peer_policy(self):
        class Sock:
            def __init__(self, ip):
                self.ip = ip

            def getpeername(self):
                return (self.ip, 443)

            def close(self):
                pass

        for ip in (
            "127.0.0.1",
            "10.0.0.1",
            "100.100.100.100",
            "169.254.169.254",
            "::1",
            "::ffff:127.0.0.1",
            "fc00::1",
        ):
            conn = sweep.PeerHTTPSConnection("example.test")
            conn.sock = Sock(ip)
            with mock.patch.object(http.client.HTTPSConnection, "connect"):
                with self.assertRaises(sweep.Refused, msg=ip):
                    conn.connect()
        conn = sweep.PeerHTTPSConnection("example.test")
        conn.sock = Sock("8.8.8.8")
        with mock.patch.object(http.client.HTTPSConnection, "connect"):
            conn.connect()

    def test_url_policy_on_every_hop_and_redirect_limit(self):
        for bad in (
            "http://example.test/a.pdf",
            "https://example.test:8080/a.pdf",
            "https://user@example.test/a.pdf",
        ):
            with self.assertRaises(sweep.Refused):
                sweep.url_host(bad)
        opener = Opener(
            [
                urllib.error.HTTPError(
                    "https://example.test/a",
                    302,
                    "redirect",
                    {"Location": "http://storage.test/x"},
                    None,
                )
            ]
        )
        with self.assertRaises(sweep.Refused):
            sweep.fetch_pdf(
                "https://example.test/a.pdf", os.path.join(self.root, "x"), opener
            )
        redirects = [
            urllib.error.HTTPError(
                "https://example.test/a", 302, "redirect", {"Location": "/b"}, None
            )
            for _ in range(6)
        ]
        with self.assertRaisesRegex(RuntimeError, "too-many-redirects"):
            sweep.fetch_pdf(
                "https://example.test/a.pdf",
                os.path.join(self.root, "x"),
                Opener(redirects),
            )

    def test_safelinks_unwrap_and_inner_host_check(self):
        wrapped = (
            "https://tenant.safelinks.protection.outlook.com/?url="
            + urllib.parse.quote("https://evil.test/invoice.pdf", safe="")
        )
        self.assertEqual(
            sweep.unwrap_safelinks(wrapped), "https://evil.test/invoice.pdf"
        )
        self.assertFalse(
            sweep.listed(
                sweep.url_host(sweep.unwrap_safelinks(wrapped)), ["example.test"]
            )
        )

    def test_stream_cap_and_html_are_removed(self):
        target = os.path.join(self.root, "target.pdf")
        huge = Response(b"%PDF-" + b"x" * sweep.MAX_BYTES)
        with self.assertRaises(OverflowError):
            sweep.fetch_pdf("https://example.test/x.pdf", target, Opener([huge]))
        self.assertFalse(os.path.exists(target))
        self.assertFalse(
            any(x.startswith(".invoice-sweep") for x in os.listdir(self.root))
        )
        with self.assertRaisesRegex(ValueError, "not-pdf"):
            sweep.fetch_pdf(
                "https://example.test/x.pdf",
                target,
                Opener([Response(b"<html>bad</html>", {"Content-Length": "16"})]),
            )
        self.assertFalse(os.path.exists(target))

    def test_no_credentials_are_sent(self):
        opener = Opener([Response()])
        target = os.path.join(self.root, "doc.pdf")
        sweep.fetch_pdf("https://example.test/invoice.pdf", target, opener)
        headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual(set(headers), {"user-agent", "accept"})

    def test_gap_only_once_and_last_date_advances(self):
        rows = [
            ("m%d" % i, "2026-10-30 12:00", "a@example.test", "x") for i in range(25)
        ]
        run, _ = self.runner(rows)
        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "blessed-mail.py",
                "2026-10-30",
                runner=run,
                today="2026-10-30",
            )
        self.assertEqual(sum("gap" in str(c) for c in output.call_args_list), 1)
        with open(self.state) as f:
            self.assertEqual(json.load(f)["last_date"], "2026-10-30")
        run, _ = self.runner(rows)
        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "blessed-mail.py",
                runner=run,
                today="2026-10-30",
            )
        self.assertFalse(
            any(str(c).startswith("call('gap") for c in output.call_args_list)
        )

    def test_failed_list_does_not_write_state(self):
        run, _ = self.runner(list_error=True)
        with self.assertRaises(RuntimeError):
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "mail.py",
                "2026-10-30",
                runner=run,
                today="2026-10-30",
            )
        self.assertFalse(os.path.exists(self.state))

    def test_dry_run_writes_nothing_and_ignore_wins(self):
        run, _ = self.runner(
            [("m1", "2026-10-30 12:00", "x@example.test", "Invoice")],
            {"m1": self.mail(sender="x@example.test")},
        )
        with open(self.config, "w") as f:
            json.dump(
                {
                    "folders": ["inbox"],
                    "hosts": [],
                    "forwarders": [],
                    "senders": [],
                    "ignore": ["X@example.test"],
                },
                f,
            )
        os.chmod(self.config, 0o600)
        sweep.run_sweep(
            self.config,
            self.incoming,
            self.state,
            self.log,
            "mail.py",
            "2026-10-30",
            True,
            run,
            today="2026-10-30",
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.log))
        self.assertFalse(os.path.exists(self.incoming))

    def test_idempotent_saved_mail_and_control_characters(self):
        row = [("m1", "2026-10-30 12:00", "vendor@example.test", "Invoice\t\n")]
        run, _ = self.runner(row, {"m1": self.mail(subject="Invoice\t\n")})
        sweep.run_sweep(
            self.config,
            self.incoming,
            self.state,
            self.log,
            "mail.py",
            "2026-10-30",
            runner=run,
            today="2026-10-30",
        )
        with open(self.log) as f:
            lines = f.readlines()
        self.assertTrue(all("\t\n" not in line.rstrip("\n") for line in lines))
        run, _ = self.runner(row, {"m1": self.mail(subject="Invoice")})
        with mock.patch("builtins.print"):
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "mail.py",
                runner=run,
                today="2026-10-30",
            )
        with open(self.log) as f:
            self.assertEqual(
                sum(line.startswith(("saved\t", "no-document\t")) for line in f), 1
            )

    def test_config_mode_is_required(self):
        os.chmod(self.config, 0o644)
        with self.assertRaisesRegex(ValueError, "mode-must-be-600"):
            sweep.load_private_config(self.config)

    def test_unlisted_link_is_reported_without_fetch(self):
        row = [("m1", "2026-10-30 12:00", "vendor@example.test", "Invoice")]
        mail = self.mail(
            links=[
                {"text": "invoice download", "href": "https://evil.test/invoice.pdf"}
            ]
        )
        run, _ = self.runner(row, {"m1": mail})
        with mock.patch("builtins.print") as output, mock.patch.object(
            sweep, "fetch_pdf"
        ) as fetch:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "mail.py",
                "2026-10-30",
                runner=run,
                today="2026-10-30",
            )
        fetch.assert_not_called()
        self.assertIn("host-unlisted", str(output.call_args_list))

    def test_open_old_mail_is_read_by_id_and_expires_after_fourteen_days(self):
        with open(self.state, "w") as f:
            json.dump(
                {
                    "last_date": "2026-10-30",
                    "mails": {
                        "old-id": {
                            "status": "open",
                            "received": "2026-10-20T12:00:00Z",
                            "first_seen": "2026-10-20",
                        }
                    },
                    "hashes": [],
                    "gaps": [],
                },
                f,
            )
        calls = []

        def run(argv, **kw):
            args = argv[1:]
            calls.append(args)
            if args[0] == "list":
                return sweep.subprocess.CompletedProcess(argv, 0, "", "")
            return sweep.subprocess.CompletedProcess(argv, 1, "", "")

        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "mail.py",
                runner=run,
                today="2026-10-30",
            )
        self.assertIn(["read", "old-id", "--json"], calls)
        self.assertNotIn("expired", str(output.call_args_list))
        with open(self.state) as f:
            self.assertEqual(json.load(f)["mails"]["old-id"]["status"], "open")

    def test_open_mail_expires_once_at_fourteen_days(self):
        with open(self.state, "w") as f:
            json.dump(
                {
                    "last_date": "2026-10-30",
                    "mails": {
                        "old-id": {
                            "status": "open",
                            "received": "2026-10-01T12:00:00Z",
                            "first_seen": "2026-10-01",
                        }
                    },
                    "hashes": [],
                    "gaps": [],
                },
                f,
            )
        run, calls = self.runner()
        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "mail.py",
                runner=run,
                today="2026-10-30",
            )
        self.assertNotIn(["read", "old-id", "--json"], calls)
        self.assertIn("expired", str(output.call_args_list))
        with open(self.state) as f:
            self.assertEqual(json.load(f)["mails"]["old-id"]["status"], "done")

    def test_duplicate_pdf_across_two_mails_is_removed(self):
        rows = [
            ("m1", "2026-10-30 12:00", "vendor@example.test", "Invoice"),
            ("m2", "2026-10-30 12:01", "vendor@example.test", "Invoice"),
        ]
        mails = {
            mid: self.mail(
                attachments=[{"name": "invoice.pdf", "size": 17, "isInline": False}]
            )
            for mid, *_ in rows
        }
        calls = []

        def run(argv, **kw):
            args = argv[1:]
            calls.append(args)
            if args[0] == "list":
                return sweep.subprocess.CompletedProcess(
                    argv,
                    0,
                    "".join(
                        "\t".join([r[0], r[1], r[2], r[3], "", "preview"]) + "\n"
                        for r in rows
                    ),
                    "",
                )
            if args[0] == "read" and len(args) > 2 and args[2] == "--json":
                return sweep.subprocess.CompletedProcess(
                    argv, 0, json.dumps(mails[args[1]]), ""
                )
            if args[0] == "read":
                os.makedirs(args[-1], exist_ok=True)
                with open(os.path.join(args[-1], args[3]), "wb") as f:
                    f.write(b"%PDF-1.7 synthetic")
                return sweep.subprocess.CompletedProcess(argv, 0, "saved", "")

        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "mail.py",
                "2026-10-30",
                runner=run,
                today="2026-10-30",
            )
        text = str(output.call_args_list)
        self.assertIn("saved", text)
        self.assertIn("dup", text)
        files = []
        for base, _, names in os.walk(self.incoming):
            files.extend(os.path.join(base, n) for n in names)
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()

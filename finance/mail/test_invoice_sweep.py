"""Synthetic tests for invoice_sweep; no mailbox, private data, or network."""

import http.client
import io
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

    def close(self):
        self.closed = True

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
                listed_rows = (
                    rows if "--search" not in args and "--from" not in args else ()
                )
                return sweep.subprocess.CompletedProcess(
                    argv,
                    0,
                    "".join(
                        "\t".join([r[0], r[1], r[2], r[3], "", "preview"]) + "\n"
                        for r in listed_rows
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
            "received": "2042-04-30T12:00:00Z",
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
            "240.0.0.1",
            "ff0e::1",
        ):
            conn = sweep.PeerHTTPSConnection("example.test")
            conn.sock = Sock(ip)
            with mock.patch.object(http.client.HTTPSConnection, "connect"):
                with self.assertRaises(sweep.Refused, msg=ip):
                    conn.connect()
        conn = sweep.PeerHTTPSConnection("example.test")
        conn.sock = Sock("::ffff:8.8.8.8")
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
                    io.BytesIO(),
                )
            ]
        )
        with self.assertRaises(sweep.Refused):
            sweep.fetch_pdf(
                "https://example.test/a.pdf", os.path.join(self.root, "x"), opener
            )
        redirects = [
            urllib.error.HTTPError(
                "https://example.test/a",
                302,
                "redirect",
                {"Location": "/b"},
                io.BytesIO(),
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
        self.assertEqual(headers["accept"], "application/pdf, */*;q=0.1")

    def test_gap_only_once_and_last_date_advances(self):
        rows = [
            ("m%d" % i, "2042-04-30 12:00", "a@example.test", "x") for i in range(25)
        ]
        run, _ = self.runner(rows)
        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "blessed-mail.py",
                "2042-04-30",
                runner=run,
                today="2042-04-30",
            )
        self.assertFalse(
            any(c.args[0].startswith("gap\t") for c in output.call_args_list)
        )
        self.assertIn("day-full=1", output.call_args_list[-1].args[0])
        with open(self.state) as f:
            self.assertEqual(json.load(f)["last_date"], "2042-04-30")
        run, _ = self.runner(rows)
        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "blessed-mail.py",
                runner=run,
                today="2042-04-30",
            )
        self.assertFalse(
            any(c.args[0].startswith("gap\t") for c in output.call_args_list)
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
                "2042-04-30",
                runner=run,
                today="2042-04-30",
            )
        self.assertFalse(os.path.exists(self.state))

    def test_dry_run_writes_nothing_and_ignore_wins(self):
        run, _ = self.runner(
            [("m1", "2042-04-30 12:00", "x@example.test", "Invoice")],
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
            "2042-04-30",
            True,
            run,
            today="2042-04-30",
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.log))
        self.assertFalse(os.path.exists(self.incoming))

    def test_idempotent_saved_mail_and_control_characters(self):
        row = [("m1", "2042-04-30 12:00", "vendor@example.test", "Invoice\t\n")]
        run, _ = self.runner(row, {"m1": self.mail(subject="Invoice\t\n")})
        sweep.run_sweep(
            self.config,
            self.incoming,
            self.state,
            self.log,
            "mail.py",
            "2042-04-30",
            runner=run,
            today="2042-04-30",
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
                today="2042-04-30",
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
        row = [("m1", "2042-04-30 12:00", "vendor@example.test", "Invoice")]
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
                "2042-04-30",
                runner=run,
                today="2042-04-30",
            )
        fetch.assert_not_called()
        self.assertIn("host-unlisted", str(output.call_args_list))

    def test_open_old_mail_is_read_by_id_and_expires_after_fourteen_days(self):
        with open(self.state, "w") as f:
            json.dump(
                {
                    "last_date": "2042-04-30",
                    "mails": {
                        "old-id": {
                            "status": "open",
                            "received": "2042-04-20T12:00:00Z",
                            "first_seen": "2042-04-20",
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
                today="2042-04-30",
            )
        self.assertIn(["read", "old-id", "--json"], calls)
        self.assertNotIn("expired", str(output.call_args_list))
        with open(self.state) as f:
            self.assertEqual(json.load(f)["mails"]["old-id"]["status"], "open")

    def test_open_mail_expires_once_at_fourteen_days(self):
        with open(self.state, "w") as f:
            json.dump(
                {
                    "last_date": "2042-04-30",
                    "mails": {
                        "old-id": {
                            "status": "open",
                            "received": "2042-04-01T12:00:00Z",
                            "first_seen": "2042-04-01",
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
                today="2042-04-30",
            )
        self.assertNotIn(["read", "old-id", "--json"], calls)
        self.assertIn("expired", str(output.call_args_list))
        with open(self.state) as f:
            self.assertEqual(json.load(f)["mails"]["old-id"]["status"], "done")

    def test_duplicate_pdf_across_two_mails_is_removed(self):
        rows = [
            ("m1", "2042-04-30 12:00", "vendor@example.test", "Invoice"),
            ("m2", "2042-04-30 12:01", "vendor@example.test", "Invoice"),
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
                "2042-04-30",
                runner=run,
                today="2042-04-30",
            )
        text = str(output.call_args_list)
        self.assertIn("saved", text)
        self.assertIn("dup", text)
        files = []
        for base, _, names in os.walk(self.incoming):
            files.extend(os.path.join(base, n) for n in names)
        self.assertEqual(len(files), 1)
        self.assertTrue(
            all(r["status"] == "done" for r in self.stored()["mails"].values())
        )

    def execute(self, runner, **kwargs):
        with mock.patch("builtins.print") as output:
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "fake-mail",
                since="2042-04-30",
                today="2042-04-30",
                runner=runner,
                **kwargs
            )
        return [c.args[0] for c in output.call_args_list]

    def stored(self):
        with open(self.state) as source:
            return json.load(source)

    def test_targeted_search_attachment_and_sender_passes_share_dedupe(self):
        with open(self.config, "w") as f:
            json.dump(
                {
                    "folders": ["inbox"],
                    "hosts": ["example.test"],
                    "forwarders": ["forwarder@example.test"],
                    "senders": ["sender@example.test"],
                    "ignore": [],
                },
                f,
            )
        os.chmod(self.config, 0o600)
        rows = {
            "invoice received:2042-04-30..2042-04-30": ["word-hit"],
            "hasAttachment:true received:2042-04-30..2042-04-30": [
                "compound",
                "word-hit",
            ],
            "from": ["forwarded", "word-hit", "compound"],
            "rechnung received:2042-04-30..2042-04-30": ["not-candidate"],
        }
        mails = {
            "word-hit": self.mail(subject="Invoice"),
            "compound": self.mail(
                subject="Monthly", attachments=[{"name": "jahresrechnung.pdf"}]
            ),
            "forwarded": self.mail(sender="forwarder@example.test", subject="Monthly"),
            "not-candidate": self.mail(subject="Unrelated"),
        }
        calls, reads = [], []

        def runner(argv, **kwargs):
            args = argv[1:]
            calls.append(args)
            if args[0] == "list":
                if "--search" in args:
                    ids = rows.get(args[args.index("--search") + 1], [])
                elif "--from" in args:
                    ids = (
                        rows["from"]
                        if args[args.index("--from") + 1] == "forwarder@example.test"
                        else []
                    )
                else:
                    ids = []
                output = "".join(
                    "\t".join(
                        [
                            mid,
                            "2042-04-30 12:00",
                            "sender@example.test",
                            "preview",
                            "",
                            "",
                        ]
                    )
                    + "\n"
                    for mid in ids
                )
                return sweep.subprocess.CompletedProcess(argv, 0, output, "")
            if "--json" in args:
                reads.append(args[1])
            return sweep.subprocess.CompletedProcess(
                argv, 0, json.dumps(mails[args[1]]), ""
            )

        lines = self.execute(runner, dry_run=True)
        self.assertCountEqual(
            reads, ["word-hit", "compound", "forwarded", "not-candidate"]
        )
        self.assertEqual(len(reads), len(set(reads)))
        self.assertFalse(any("not-candidate" in line for line in lines))
        outcomes = [line.split("\t") for line in lines[:-1]]
        self.assertCountEqual(
            [(row[0], row[2], row[3]) for row in outcomes],
            [
                ("no-document", "vendor@example.test", "Invoice"),
                ("saved", "vendor@example.test", "Monthly"),
                ("no-document", "forwarder@example.test", "Monthly"),
            ],
        )
        self.assertEqual(os.listdir(self.root), ["sweep.json"])
        expected = [
            [
                "list",
                "--folder",
                "inbox",
                "--since",
                "2042-04-30",
                "--before",
                "2042-05-01",
                "--top",
                "25",
            ]
        ]
        expected.extend(
            [
                "list",
                "--folder",
                "inbox",
                "--search",
                "%s received:2042-04-30..2042-04-30" % word,
                "--top",
                "25",
            ]
            for word in sweep.WORDS
        )
        expected.append(
            [
                "list",
                "--folder",
                "inbox",
                "--search",
                "hasAttachment:true received:2042-04-30..2042-04-30",
                "--top",
                "25",
            ]
        )
        expected.extend(
            [
                "list",
                "--folder",
                "inbox",
                "--from",
                address,
                "--since",
                "2042-04-30",
                "--before",
                "2042-05-01",
                "--top",
                "25",
            ]
            for address in ("forwarder@example.test", "sender@example.test")
        )
        self.assertEqual(calls[: len(expected)], expected)

    def test_targeted_gap_is_deduped_and_old_gap_shape_loads(self):
        with open(self.state, "w") as f:
            json.dump(
                {
                    "last_date": "2042-04-30",
                    "mails": {},
                    "hashes": [],
                    "gaps": [["inbox", "2042-04-30"]],
                },
                f,
            )
        rows = [("same", "2042-04-30 12:00", "x@example.test", "Preview")] * 25
        base, _ = self.runner(mails={"same": self.mail(subject="Hello")})

        def runner(argv, **kwargs):
            args = argv[1:]
            if (
                args[0] == "list"
                and "--search" in args
                and args[args.index("--search") + 1].startswith("invoice ")
            ):
                return sweep.subprocess.CompletedProcess(
                    argv,
                    0,
                    "".join("\t".join([*row, "", ""]) + "\n" for row in rows),
                    "",
                )
            return base(argv, **kwargs)

        first = self.execute(runner)
        gaps = [line for line in first if line.startswith("gap\t")]
        self.assertEqual(len(gaps), 1)
        self.assertIn("invoice received:2042-04-30..2042-04-30", gaps[0])
        self.assertEqual(self.stored()["gaps"][0], ["inbox", "2042-04-30"])
        second = self.execute(runner)
        self.assertFalse(any(line.startswith("gap\t") for line in second))

    def test_sender_gap_and_failure_preserve_state(self):
        with open(self.config) as source:
            cfg = json.load(source)
        cfg["senders"] = ["sender@example.test"]
        with open(self.config, "w") as target:
            json.dump(cfg, target)
        base, _ = self.runner(mails={"same": self.mail(subject="Hello")})
        fail = False

        def runner(argv, **kwargs):
            args = argv[1:]
            if args[0] == "list" and "--from" in args:
                return sweep.subprocess.CompletedProcess(
                    argv,
                    1 if fail else 0,
                    "same\t2042-04-30 12:00\tx@example.test\tHello\n" * 25,
                    "",
                )
            return base(argv, **kwargs)

        first = self.execute(runner)
        gaps = [line for line in first if line.startswith("gap\t")]
        self.assertEqual(len(gaps), 1)
        self.assertIn("inbox from sender@example.test", gaps[0])
        self.assertFalse(any(line.startswith("gap\t") for line in self.execute(runner)))
        with open(self.state, "rb") as source:
            before = source.read()
        fail = True
        with self.assertRaisesRegex(RuntimeError, "mail-list-failed"):
            self.execute(runner)
        with open(self.state, "rb") as source:
            self.assertEqual(source.read(), before)

    def test_earlier_since_rewinds_but_later_since_does_not(self):
        with open(self.state, "w") as f:
            json.dump(
                {"last_date": "2042-04-30", "mails": {}, "hashes": [], "gaps": []}, f
            )
        run, calls = self.runner()
        sweep.run_sweep(
            self.config,
            self.incoming,
            self.state,
            self.log,
            "fake-mail",
            "2042-04-28",
            runner=run,
            today="2042-04-30",
        )
        self.assertEqual(calls[0][calls[0].index("--since") + 1], "2042-04-28")
        calls.clear()
        sweep.run_sweep(
            self.config,
            self.incoming,
            self.state,
            self.log,
            "fake-mail",
            "2042-05-01",
            runner=run,
            today="2042-04-30",
        )
        self.assertEqual(calls[0][calls[0].index("--since") + 1], "2042-04-30")

    def test_failed_search_does_not_advance_or_rewrite_state(self):
        original = {"last_date": "2042-04-30", "mails": {}, "hashes": [], "gaps": []}
        with open(self.state, "w") as f:
            json.dump(original, f)
        with open(self.state, "rb") as f:
            before = f.read()
        base, _ = self.runner()

        def runner(argv, **kwargs):
            args = argv[1:]
            if args[0] == "list" and "--search" in args:
                return sweep.subprocess.CompletedProcess(
                    argv, 1, "", "synthetic failure"
                )
            return base(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, "mail-list-failed"):
            sweep.run_sweep(
                self.config,
                self.incoming,
                self.state,
                self.log,
                "fake-mail",
                runner=runner,
                today="2042-04-30",
            )
        with open(self.state, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_duplicate_links_are_removed_and_done_on_rerun(self):
        rows = [
            (mid, "2042-04-30", "vendor@example.test", "Invoice") for mid in ("a", "b")
        ]
        mails = {
            mid: self.mail(links=[{"href": "https://example.test/invoice.pdf"}])
            for mid in ("a", "b")
        }
        runner, calls = self.runner(rows, mails)
        opener = Opener([Response(), Response()])
        lines = self.execute(runner, opener_factory=lambda: opener)
        self.assertEqual(
            [line.split("\t")[0] for line in lines], ["saved", "dup", "totals"]
        )
        self.assertTrue(
            all(r["status"] == "done" for r in self.stored()["mails"].values())
        )
        self.assertEqual(sum(len(files) for _, _, files in os.walk(self.incoming)), 1)
        calls.clear()
        self.assertEqual(self.execute(runner), ["totals\tday-full=0"])
        self.assertTrue(all(call[0] == "list" for call in calls))

    def test_partial_attachment_failure_retries_only_failed_document(self):
        mail = self.mail(
            attachments=[
                {"name": "first.pdf"},
                {"name": "second.pdf"},
                {"name": "forward.eml"},
            ]
        )
        base, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )
        attempts = []
        fail = [True]

        def runner(argv, **kwargs):
            if "--attachment" not in argv:
                return base(argv, **kwargs)
            name, outdir = argv[4], argv[-1]
            attempts.append(name)
            if name == "second.pdf" and fail[0]:
                return sweep.subprocess.CompletedProcess(argv, 1, "", "")
            with open(os.path.join(outdir, name), "wb") as out:
                out.write(b"%PDF-synthetic-" + name.encode())
            return sweep.subprocess.CompletedProcess(argv, 0, "", "")

        first = self.execute(runner)
        self.assertEqual(self.stored()["mails"]["a"]["status"], "open")
        fail[0] = False
        second = self.execute(runner)
        self.assertEqual(attempts, ["first.pdf", "second.pdf", "second.pdf"])
        self.assertEqual(self.stored()["mails"]["a"]["status"], "done")
        self.assertEqual(
            sum(x.startswith("other-attachment\t") for x in first + second), 1
        )
        self.assertEqual(sum(x.startswith("saved\t") for x in first + second), 2)
        for base_dir, _, names in os.walk(self.incoming):
            self.assertEqual(stat.S_IMODE(os.stat(base_dir).st_mode), 0o700)
            for name in names:
                self.assertEqual(
                    stat.S_IMODE(os.stat(os.path.join(base_dir, name)).st_mode), 0o600
                )
        for path in (self.state, self.log, self.state + ".lock"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_partial_link_failure_retries_only_failed_link(self):
        mail = self.mail(
            links=[
                {"href": "https://example.test/invoice-%d.pdf" % i} for i in range(2)
            ]
        )
        runner, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )
        opener = Opener([Response(), OSError("synthetic"), Response(b"%PDF-different")])
        self.execute(runner, opener_factory=lambda: opener)
        self.assertEqual(self.stored()["mails"]["a"]["status"], "open")
        self.execute(runner, opener_factory=lambda: opener)
        self.assertEqual(
            [r.full_url for r in opener.requests],
            [
                "https://example.test/invoice-0.pdf",
                "https://example.test/invoice-1.pdf",
                "https://example.test/invoice-1.pdf",
            ],
        )
        self.assertEqual(self.stored()["mails"]["a"]["status"], "done")

    def test_link_cap_counts_qualifying_links_after_unrelated_links(self):
        mail = self.mail(
            links=[{"href": "https://example.test/help"}] * 4
            + [{"href": "https://example.test/invoice-%d.pdf" % i} for i in range(4)]
        )
        runner, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )
        opener = Opener([Response()] * 3)
        self.execute(runner, opener_factory=lambda: opener)
        self.assertEqual(len(opener.requests), 3)
        self.assertTrue(opener.requests[0].full_url.endswith("invoice-0.pdf"))

    def test_safe_links_inner_host_enforced_end_to_end(self):
        wrapped = (
            "https://tenant.safelinks.protection.outlook.com/?url="
            + urllib.parse.quote("https://unlisted.test/invoice.pdf", safe="")
        )
        mail = self.mail(links=[{"href": wrapped}])
        runner, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )
        opener = Opener([])
        lines = self.execute(runner, opener_factory=lambda: opener)
        self.assertTrue(lines[0].startswith("host-unlisted\t"))
        self.assertFalse(opener.requests)
        self.assertEqual(self.stored()["mails"]["a"]["status"], "open")

    def test_failed_read_of_new_mail_is_retried_then_expires(self):
        runner, _ = self.runner([("a", "2042-04-30", "v@example.test", "Invoice")])
        self.execute(runner)
        state = self.stored()
        state["mails"]["a"]["received"] = "2042-04-01"
        with open(self.state, "w") as out:
            json.dump(state, out)
        runner, _ = self.runner()
        lines = self.execute(runner)
        self.assertEqual([x.split("\t")[0] for x in lines], ["expired", "totals"])
        self.assertEqual(self.execute(runner), ["totals\tday-full=0"])

    def test_non_candidates_are_remembered_and_quotes_in_tsv_are_literal(self):
        rows = [
            ("a", "2042-04-30", "v@example.test", '"unclosed quote'),
            ("b", "2042-04-30", "v@example.test", "Invoice"),
        ]
        runner, calls = self.runner(
            rows, {"a": self.mail(subject="Hello"), "b": self.mail()}
        )
        self.execute(runner)
        self.assertEqual(set(self.stored()["mails"]), {"a", "b"})
        calls.clear()
        self.execute(runner)
        self.assertTrue(all(call[0] == "list" for call in calls))

    def test_fetch_closes_response_and_rejects_length_before_read(self):
        response = Response(headers={"Content-Length": str(sweep.MAX_BYTES + 1)})
        target = os.path.join(self.root, "x.pdf")
        with self.assertRaises(OverflowError):
            sweep.fetch_pdf("https://example.test/a.pdf", target, Opener([response]))
        self.assertTrue(response.closed)
        self.assertEqual(response.offset, 0)
        self.assertFalse(os.path.exists(target))

    def test_body_read_error_is_reported_and_partial_file_removed(self):
        response = Response()
        response.read = mock.Mock(
            side_effect=[b"%PDF-partial", http.client.IncompleteRead(b"partial")]
        )
        mail = self.mail(links=[{"href": "https://example.test/invoice.pdf"}])
        runner, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )
        lines = self.execute(runner, opener_factory=lambda: Opener([response]))
        self.assertTrue(lines[0].startswith("fetch-failed\t"))
        self.assertEqual(self.stored()["mails"]["a"]["status"], "open")
        self.assertTrue(response.closed)
        self.assertEqual(sum(len(files) for _, _, files in os.walk(self.incoming)), 0)

    def test_deadline_after_final_read_removes_partial_file(self):
        response = Response()
        clock = mock.Mock(side_effect=[0, 0, 0, 1, 1, 121])
        target = os.path.join(self.root, "x.pdf")
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            sweep.fetch_pdf(
                "https://example.test/a.pdf", target, Opener([response]), clock
            )
        self.assertTrue(response.closed)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(
            any(n.startswith(".invoice-sweep") for n in os.listdir(self.root))
        )

    def test_deadline_interrupts_a_blocked_read_and_restores_handler(self):
        previous = sweep.signal.getsignal(sweep.signal.SIGALRM)
        response = Response()

        def interrupted(n):
            # Simulate the OS delivering the installed deadline signal mid-read.
            sweep.signal.getsignal(sweep.signal.SIGALRM)(sweep.signal.SIGALRM, None)

        response.read = interrupted
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            sweep.fetch_pdf(
                "https://example.test/a.pdf",
                os.path.join(self.root, "x"),
                Opener([response]),
            )
        self.assertEqual(sweep.signal.getsignal(sweep.signal.SIGALRM), previous)
        self.assertEqual(sweep.signal.getitimer(sweep.signal.ITIMER_REAL), (0, 0))
        self.assertTrue(response.closed)

    def test_redirect_to_storage_host_and_security_policy(self):
        error = urllib.error.HTTPError(
            "https://example.test/a.pdf",
            302,
            "redirect",
            {"Location": "https://storage.test/b"},
            io.BytesIO(),
        )
        opener = Opener([error, Response()])
        sweep.fetch_pdf(
            "https://example.test/a.pdf", os.path.join(self.root, "x"), opener
        )
        self.assertEqual(opener.requests[1].full_url, "https://storage.test/b")
        for bad in (
            "https://example.test:wrong/a.pdf",
            "https://[bad/a.pdf",
            "https://é.test/a.pdf",
            "https://u:p@example.test/a.pdf",
        ):
            with self.subTest(url=bad), self.assertRaises(sweep.Refused):
                sweep.url_host(bad)
        self.assertTrue(sweep.listed("child.example.test", ["example.test"]))
        self.assertFalse(sweep.listed("fakeexample.test", ["example.test"]))
        handlers = sweep.safe_opener().handlers
        self.assertFalse(
            any(
                isinstance(
                    h, (urllib.request.HTTPCookieProcessor, urllib.request.ProxyHandler)
                )
                for h in handlers
            )
        )

    def test_config_missing_lock_and_mail_subprocess_contract(self):
        with self.assertRaisesRegex(ValueError, '"folders"'):
            sweep.load_private_config(os.path.join(self.root, "missing"))
        runner = mock.Mock(
            return_value=sweep.subprocess.CompletedProcess([], 0, "", "")
        )
        sweep.mail_call(sweep.MAIL, ["list"], runner)
        runner.assert_called_once_with(
            [sweep.MAIL, "list"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        with open(self.state + ".lock", "w") as lock:
            sweep.fcntl.flock(lock, sweep.fcntl.LOCK_EX | sweep.fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "already-running"):
                self.execute(self.runner()[0])

    def test_dry_run_explains_attachments_and_links_without_writes(self):
        mail = self.mail(
            attachments=[{"name": "bill.pdf"}, {"name": "forward.eml"}],
            links=[{"href": "https://unlisted.test/invoice.pdf"}],
        )
        runner, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )
        before = os.listdir(self.root)
        with mock.patch.object(sweep, "fetch_pdf") as fetch:
            lines = self.execute(runner, dry_run=True)
        fetch.assert_not_called()
        self.assertEqual(before, os.listdir(self.root))
        self.assertEqual(
            [x.split("\t")[0] for x in lines],
            ["saved", "other-attachment", "host-unlisted", "totals"],
        )
        self.assertIn("would inspect", lines[0])

    def test_controls_are_removed_and_subject_is_capped(self):
        value = sweep.clean("Invoice\x00\x1b\t\n" + "x" * 180, 120)
        self.assertEqual(len(value), 120)
        self.assertTrue(all(ord(c) >= 32 and ord(c) != 127 for c in value))

    def test_attachment_names_cannot_collide_with_final_names(self):
        names = ["attachment-2.pdf", "attachment-1.pdf"]
        mail = self.mail(
            attachments=[{"name": n} for n in names],
            links=[{"href": "https://example.test/invoice.pdf"}],
        )
        base, _ = self.runner(
            [("a", "2042-04-30", "v@example.test", "Invoice")], {"a": mail}
        )

        def runner(argv, **kwargs):
            if "--attachment" not in argv:
                return base(argv, **kwargs)
            name, outdir = argv[4], argv[-1]
            path = os.path.join(outdir, name)
            if os.path.exists(path):
                return sweep.subprocess.CompletedProcess(argv, 2, "", "exists")
            with open(path, "wb") as out:
                out.write(b"%PDF-" + name.encode())
            return sweep.subprocess.CompletedProcess(argv, 0, "", "")

        opener = Opener([])
        self.execute(runner, opener_factory=lambda: opener)
        self.assertFalse(opener.requests)
        contents = []
        for base_dir, _, files in os.walk(self.incoming):
            for name in files:
                with open(os.path.join(base_dir, name), "rb") as source:
                    contents.append(source.read())
        self.assertCountEqual(contents, [b"%PDF-" + n.encode() for n in names])

    def test_candidate_rules_and_ignore_override(self):
        cases = [
            (
                self.mail(
                    subject="Hello",
                    attachments=[{"name": "invoice.png", "isInline": True}],
                ),
                True,
            ),
            (self.mail(subject="Hello", links=[{"text": "Facture"}]), True),
            (
                self.mail(
                    subject="Hello", text="Rechnung", attachments=[{"name": "data.PDF"}]
                ),
                True,
            ),
            (self.mail(subject="Hello", text="Invoice"), False),
            (
                self.mail(
                    subject="Hello",
                    text="Invoice",
                    attachments=[{"name": "data.pdf", "isInline": True}],
                ),
                False,
            ),
        ]
        for mail, expected in cases:
            with self.subTest(mail=mail):
                runner, _ = self.runner(
                    [("a", "2042-04-30", "v@example.test", "Hello")], {"a": mail}
                )
                lines = self.execute(runner, dry_run=True)
                self.assertEqual(len(lines) > 1, expected)
        for configured in ("senders", "forwarders", "ignore"):
            with open(self.config, "w") as out:
                json.dump(
                    {"folders": ["inbox"], configured: ["VENDOR@example.test"]}, out
                )
            runner, _ = self.runner(
                [("a", "2042-04-30", "v@example.test", "Hello")],
                {"a": self.mail(subject="Hello")},
            )
            lines = self.execute(runner, dry_run=True)
            self.assertEqual(len(lines) > 1, configured != "ignore")

    def test_existing_state_is_unchanged_when_later_listing_fails(self):
        original = {"last_date": "2042-04-29", "mails": {}, "hashes": [], "gaps": []}
        with open(self.state, "w") as out:
            json.dump(original, out)
        with open(self.state, "rb") as source:
            before = source.read()
        replies = iter(
            [
                sweep.subprocess.CompletedProcess([], 0, "", ""),
                sweep.subprocess.CompletedProcess([], 1, "", ""),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "mail-list-failed"):
            self.execute(lambda *args, **kwargs: next(replies))
        with open(self.state, "rb") as source:
            self.assertEqual(before, source.read())
        self.assertFalse(os.path.exists(self.log))

    def test_expiry_boundary_and_pruning_preserve_hashes(self):
        with open(self.state, "w") as out:
            json.dump(
                {
                    "last_date": "2042-04-30",
                    "mails": {
                        "expired": {
                            "status": "open",
                            "received": "2042-04-16",
                            "first_seen": "2042-04-16",
                        },
                        "old": {
                            "status": "done",
                            "received": "2041-01-01",
                            "first_seen": "2041-01-01",
                        },
                    },
                    "hashes": ["synthetic-digest"],
                    "gaps": [],
                },
                out,
            )
        runner, calls = self.runner()
        lines = self.execute(runner)
        self.assertTrue(lines[0].startswith("expired\t"))
        self.assertTrue(all(call[0] == "list" for call in calls))
        state = self.stored()
        self.assertNotIn("old", state["mails"])
        self.assertEqual(state["hashes"], ["synthetic-digest"])
        self.assertEqual(state["mails"]["expired"]["status"], "done")


if __name__ == "__main__":
    unittest.main()

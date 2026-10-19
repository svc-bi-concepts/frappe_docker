"""The mail tool's tests, on a stubbed HTTP layer: no network, no real key, ids or mail.

Every address, name and body here is invented. Standard library only, plus openssl for the
signing check. The launcher's python body is loaded the way the SharePoint tests load theirs.
"""

import base64
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
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(filename):
    """Run the launcher's python body as a module: it sits between the heredoc markers."""
    name = filename[: -len(".py")]
    path = os.path.join(HERE, filename)
    with open(path) as f:
        lines = f.read().split("\n")
    start = next(
        i for i, line in enumerate(lines) if line.startswith("exec ") and "<<" in line
    )
    tag = lines[start].split("<<", 1)[1].strip().strip("'")
    end = lines.index(tag, start + 1)
    module = sys.modules[name] = types.ModuleType(name)
    module.__file__ = path
    exec(
        compile("\n".join(lines[start + 1 : end]) + "\n", path, "exec"), module.__dict__
    )
    return module


mail = _load("mail.py")
from mail import GRAPH, Graph, GraphError, MailError  # noqa: E402

CFG = {
    "tenant_id": "tenant-x",
    "client_id": "client-x",
    "thumbprint": "00" * 20,
    "mailbox": "invented.person@example.test",
}
PREFIX = "%s/users/invented.person%%40example.test/" % GRAPH
SIG_HTML = '<div id="Signature"><img src="cid:logo.png@01AB.CD"> Invented Signer</div>'


class Fake:
    """Answers each request with the next canned reply; records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        reply = self.replies.pop(0) if self.replies else {}
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode()

    def sent(self, method):
        return [c for c in self.calls if c[0] == method]


def graph_with(*replies):
    fake = Fake(*replies)
    g = Graph(CFG, b"unused", transport=fake)
    g.token = "token-x"  # the token call is tested on its own
    return g, fake


def signature_dir(tmp, sig=SIG_HTML, images=None):
    d = tempfile.mkdtemp(dir=tmp)
    with open(os.path.join(d, "signature.html"), "w") as f:
        f.write(sig)
    for name, data in (
        images if images is not None else {"logo.png@01AB.CD": b"\x89PNGinvented"}
    ).items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
    return d


class SignatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_images_referenced_by_the_html_are_loaded(self):
        d = signature_dir(self.tmp)
        sig, images = mail.load_signature(d)
        self.assertEqual(sig, SIG_HTML)
        self.assertEqual(images, [("logo.png@01AB.CD", b"\x89PNGinvented")])
        self.assertEqual(len(images), 1)

    def test_missing_image_is_refused_before_any_graph_call(self):
        d = signature_dir(self.tmp, images={})
        with self.assertRaises(MailError) as ctx:
            mail.load_signature(d)
        self.assertEqual(ctx.exception.code, 2)

    def test_unsafe_cid_is_refused(self):
        sig = '<img src="cid:../escape.png">'
        d = signature_dir(self.tmp, sig=sig, images={})
        with self.assertRaises(MailError):
            mail.load_signature(d)

    def test_oversize_image_is_refused(self):
        d = signature_dir(
            self.tmp, images={"logo.png@01AB.CD": b"x" * (mail.MAX_IMG + 1)}
        )
        with self.assertRaises(MailError):
            mail.load_signature(d)


class DraftNewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sig_dir = signature_dir(self.tmp)
        self.patch = unittest.mock.patch.object(mail, "SIGNATURE_DIR", self.sig_dir)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_body_is_html_text_then_signature(self):
        g, fake = graph_with(
            {"id": "draft-1", "isDraft": True, "webLink": "https://example.test/d1"}, {}
        )
        did, link = mail.draft_new(
            g,
            {
                "subject": "Invented subject",
                "to": ["a@example.test"],
                "body": "Hello <b>there</b>\nSecond line",
            },
        )
        self.assertEqual((did, link), ("draft-1", "https://example.test/d1"))
        ((method, url, _, data),) = fake.sent("POST")[:1]
        self.assertEqual(url, PREFIX + "messages")
        msg = json.loads(data)
        self.assertEqual(msg["body"]["contentType"], "HTML")
        content = msg["body"]["content"]
        self.assertIn("Hello &lt;b&gt;there&lt;/b&gt;<br>\nSecond line", content)
        self.assertLess(content.index("Hello"), content.index(SIG_HTML))
        self.assertEqual(
            msg["toRecipients"], [{"emailAddress": {"address": "a@example.test"}}]
        )
        self.assertNotIn("ccRecipients", msg)

    def test_signature_image_is_posted_inline_with_its_cid(self):
        g, fake = graph_with({"id": "draft-1", "isDraft": True}, {})
        mail.draft_new(g, {"subject": "S", "to": ["a@example.test"], "body": "x"})
        posts = fake.sent("POST")
        self.assertEqual(posts[1][1], PREFIX + "messages/draft-1/attachments")
        att = json.loads(posts[1][3])
        self.assertTrue(att["isInline"])
        self.assertEqual(att["contentId"], "logo.png@01AB.CD")
        self.assertEqual(att["name"], "logo.png")
        self.assertEqual(att["contentType"], "image/png")
        self.assertEqual(base64.b64decode(att["contentBytes"]), b"\x89PNGinvented")

    def test_cc_is_sent_when_given(self):
        g, fake = graph_with({"id": "draft-1", "isDraft": True}, {})
        mail.draft_new(
            g,
            {
                "subject": "S",
                "to": ["a@example.test"],
                "cc": ["b@example.test"],
                "body": "x",
            },
        )
        msg = json.loads(fake.sent("POST")[0][3])
        self.assertEqual(
            msg["ccRecipients"], [{"emailAddress": {"address": "b@example.test"}}]
        )

    def test_bad_recipient_is_refused_before_any_call(self):
        g, fake = graph_with()
        with self.assertRaises(MailError):
            mail.draft_new(g, {"subject": "S", "to": ["not an address"], "body": "x"})
        self.assertEqual(fake.calls, [])

    def test_missing_signature_image_leaves_no_draft(self):
        bare = signature_dir(self.tmp, images={})
        g, fake = graph_with()
        with unittest.mock.patch.object(mail, "SIGNATURE_DIR", bare):
            with self.assertRaises(MailError):
                mail.draft_new(
                    g, {"subject": "S", "to": ["a@example.test"], "body": "x"}
                )
        self.assertEqual(fake.calls, [])


class DraftReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.patch = unittest.mock.patch.object(
            mail, "SIGNATURE_DIR", signature_dir(self.tmp)
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def reply(self, reply_all=False):
        created = {
            "id": "reply-1",
            "isDraft": True,
            "body": {
                "contentType": "HTML",
                "content": "<html><head></head><body><div>QUOTED HISTORY</div></body></html>",
            },
        }
        patched = {
            "id": "reply-1",
            "isDraft": True,
            "webLink": "https://example.test/r1",
        }
        g, fake = graph_with(created, patched, {})
        result = mail.draft_reply(g, "source-9", reply_all, {"body": "Our answer"})
        return result, fake

    def test_reply_text_and_signature_sit_above_the_quoted_history(self):
        (did, link), fake = self.reply()
        self.assertEqual((did, link), ("reply-1", "https://example.test/r1"))
        patch = [c for c in fake.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[1], PREFIX + "messages/reply-1")
        content = json.loads(patch[3])["body"]["content"]
        ours = content.index("Our answer")
        sig = content.index(SIG_HTML)
        quoted = content.index("QUOTED HISTORY")
        self.assertLess(content.index("<body>"), ours)
        self.assertLess(ours, sig)
        self.assertLess(sig, quoted)

    def test_reply_is_created_with_createreply_and_not_sent(self):
        _, fake = self.reply()
        self.assertEqual(fake.calls[0][1], PREFIX + "messages/source-9/createReply")
        self.assertFalse(any("send" in c[1] for c in fake.calls))

    def test_reply_all_uses_createreplyall(self):
        _, fake = self.reply(reply_all=True)
        self.assertEqual(fake.calls[0][1], PREFIX + "messages/source-9/createReplyAll")

    def test_createreply_answering_with_the_source_id_is_refused(self):
        g, fake = graph_with(
            {"id": "source-9", "isDraft": True, "body": {"content": ""}}
        )
        with self.assertRaises(MailError):
            mail.draft_reply(g, "source-9", False, {"body": "x"})
        self.assertEqual(len(fake.calls), 1)  # nothing patched

    def test_reply_body_without_a_body_tag_puts_our_text_first(self):
        self.assertEqual(
            mail.with_block("<div>QUOTE</div>", "OURS"), "OURS<div>QUOTE</div>"
        )


class ListTest(unittest.TestCase):
    def test_top_is_capped_at_25_before_any_call(self):
        with self.assertRaises(SystemExit), unittest.mock.patch(
            "sys.stderr", new=io.StringIO()
        ):
            mail.parser().parse_args(["list", "--top", "26"])
        self.assertEqual(mail.parser().parse_args(["list", "--top", "25"]).top, 25)

    def test_top_request_is_one_call_and_the_next_link_is_not_followed(self):
        g, fake = graph_with(
            {
                "value": [],
                "@odata.nextLink": PREFIX + "mailFolders/inbox/messages?$skip=25",
            }
        )
        mail.list_rows(g, "inbox", 25)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("%24top=25", fake.calls[0][1])

    def test_from_and_since_become_one_filter(self):
        g, fake = graph_with({"value": []})
        mail.list_rows(
            g, "inbox", 5, from_who="o'brien@example.test", since="2026-10-01"
        )
        url = fake.calls[0][1]
        self.assertIn("o%27%27brien", url)
        self.assertIn("receivedDateTime%20ge%202026-10-01T00%3A00%3A00Z", url)

    def test_search_does_not_combine_with_from(self):
        g, fake = graph_with()
        with self.assertRaises(MailError) as ctx:
            mail.list_rows(g, "inbox", 5, from_who="x@example.test", search="invoice")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(fake.calls, [])


class ContactsTest(unittest.TestCase):
    def test_403_is_exit_3_with_a_clear_message(self):
        g, _ = graph_with(GraphError("GET", 403, "Forbidden"))
        with self.assertRaises(MailError) as ctx:
            mail.contact_rows(g)
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("Contacts.Read", str(ctx.exception))

    def test_contacts_come_back_as_name_and_address(self):
        g, _ = graph_with(
            {
                "value": [
                    {
                        "displayName": "Invented Person",
                        "emailAddresses": [{"address": "p@example.test"}],
                    }
                ]
            }
        )
        self.assertEqual(mail.contact_rows(g), [("Invented Person", "p@example.test")])


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.g, self.fake = graph_with()

    def test_send_is_refused(self):
        with self.assertRaises(MailError):
            self.g._send("POST", PREFIX + "messages/m1/send", {}, b"{}")
        self.assertEqual(self.fake.calls, [])

    def test_delete_is_refused(self):
        with self.assertRaises(MailError):
            self.g._send("DELETE", PREFIX + "messages/m1", {})

    def test_another_mailbox_is_refused(self):
        with self.assertRaises(MailError):
            self.g._send("GET", GRAPH + "/users/someone-else@example.test/messages", {})

    def test_post_to_an_existing_message_other_than_reply_or_attach_is_refused(self):
        with self.assertRaises(MailError):
            self.g._send("POST", PREFIX + "messages/m1/move", {}, b"{}")

    def test_reads_are_allowed(self):
        self.g._send("GET", PREFIX + "messages/m1", {})
        self.assertEqual(len(self.fake.calls), 1)


class ReadTest(unittest.TestCase):
    def test_html_becomes_text_without_script_or_tags(self):
        text = mail.html_to_text(
            "<html><head><title>T</title></head><body><script>x()</script>"
            "<p>Hello &amp; welcome</p><div>Line two</div></body></html>"
        )
        self.assertEqual(text, "Hello & welcome\n\nLine two")

    def test_attachment_is_saved_once_under_out_dir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        data = b"invented file bytes"
        g, _ = graph_with(
            {"value": [{"id": "a1", "name": "note.txt", "size": len(data)}]},
            {"id": "a1", "contentBytes": base64.b64encode(data).decode()},
        )
        path, size = mail.save_attachment(g, "m1", "note.txt", os.path.join(tmp, "out"))
        self.assertEqual(size, len(data))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with self.assertRaises(MailError):
            mail.save_attachment(g, "m1", "note.txt", os.path.join(tmp, "out"))

    def test_a_name_that_could_leave_the_directory_is_refused(self):
        g, fake = graph_with()
        with self.assertRaises(MailError):
            mail.save_attachment(g, "m1", "../escape.txt", "/tmp/unused")
        self.assertEqual(fake.calls, [])


class ConfigTest(unittest.TestCase):
    def test_config_readable_by_others_is_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "mail.json")
        with open(path, "w") as f:
            json.dump(CFG, f)
        os.chmod(path, 0o644)
        with self.assertRaises(MailError):
            mail.load_config(path)
        os.chmod(path, 0o600)
        self.assertEqual(mail.load_config(path)["client_id"], "client-x")


class AssertionTest(unittest.TestCase):
    def test_client_assertion_verifies_against_the_public_key(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        key, cert = os.path.join(tmp, "k.pem"), os.path.join(tmp, "c.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                cert,
                "-subj",
                "/CN=invented",
                "-days",
                "1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(key) as f:
            key_pem = f.read()
        g = Graph(CFG, key_pem, transport=Fake(), now=lambda: 1000)
        assertion = g.client_assertion()
        signing, sig = assertion.rsplit(".", 1)
        pad = "=" * (-len(sig) % 4)
        raw = base64.urlsafe_b64decode(sig + pad)
        sig_path = os.path.join(tmp, "sig.bin")
        with open(sig_path, "wb") as f:
            f.write(raw)
        pub = subprocess.run(
            ["openssl", "x509", "-in", cert, "-pubkey", "-noout"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        pub_path = os.path.join(tmp, "pub.pem")
        with open(pub_path, "wb") as f:
            f.write(pub)
        msg_path = os.path.join(tmp, "msg.txt")
        with open(msg_path, "w") as f:
            f.write(signing)
        check = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                pub_path,
                "-signature",
                sig_path,
                msg_path,
            ],
            capture_output=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()

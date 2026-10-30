"""The mail tool's tests, on a stubbed HTTP layer: no network, no real key, ids or mail.

Every address, name and body here is invented. Standard library only, plus openssl for the
signing check. The launcher's python body is loaded the way the SharePoint tests load theirs.
"""

import base64
import concurrent.futures
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
import urllib.parse

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


class GraphAttachmentSelectFake(Fake):
    """Model Graph rejecting contentId on the base attachment collection."""

    def __call__(self, method, url, headers, data):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if (
            method == "GET"
            and "/attachments" in urllib.parse.urlsplit(url).path
            and "contentId" in query.get("$select", [""])[0].split(",")
        ):
            self.calls.append((method, url, headers, data))
            raise GraphError("GET", 400, "BadRequest")
        return super().__call__(method, url, headers, data)


def graph_with(*replies):
    fake = Fake(*replies)
    g = Graph(CFG, b"unused", transport=fake)
    g.token = "token-x"  # the token call is tested on its own
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    g.drafts_path = os.path.join(tmp.name, "mail-drafts.json")
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
        self.assertEqual(mail.load_drafts(g.drafts_path)[0]["id"], "draft-1")
        self.assertEqual(mail.load_drafts(g.drafts_path)[0]["kind"], "new")

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

    def test_image_failure_leaves_new_draft_tracked(self):
        g, _ = graph_with(
            {"id": "draft-1", "isDraft": True}, GraphError("POST", 400, "Invalid")
        )
        with self.assertRaises(MailError):
            mail.draft_new(
                g, {"subject": "Invented", "to": ["a@example.test"], "body": "Invented"}
            )
        record = mail.load_drafts(g.drafts_path)[0]
        self.assertEqual((record["id"], record["kind"]), ("draft-1", "new"))
        self.assertIsNotNone(
            mail.datetime.datetime.fromisoformat(record["created"]).tzinfo
        )

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
        self.last_graph = g
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
        (did, _), fake = self.reply()
        record = mail.load_drafts(self.last_graph.drafts_path)[0]
        self.assertEqual((record["id"], record["kind"]), (did, "reply"))
        self.assertEqual(fake.calls[0][1], PREFIX + "messages/source-9/createReply")
        self.assertFalse(any("send" in c[1] for c in fake.calls))

    def test_failed_reply_patch_leaves_reply_tracked(self):
        g, _ = graph_with(
            {"id": "reply-1", "isDraft": True, "body": {"content": "Invented quote"}},
            GraphError("PATCH", 409, "Conflict"),
        )
        with self.assertRaises(GraphError):
            mail.draft_reply(g, "source-9", False, {"body": "Invented answer"})
        record = mail.load_drafts(g.drafts_path)[0]
        self.assertEqual((record["id"], record["kind"]), ("reply-1", "reply"))

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

    def test_plain_text_original_is_kept_as_escaped_html_with_line_breaks(self):
        created = {
            "id": "reply-1",
            "isDraft": True,
            "body": {"contentType": "Text", "content": "Quoted <one>\nQuoted two"},
        }
        g, fake = graph_with(created, {"id": "reply-1"}, {})
        mail.draft_reply(g, "source-9", False, {"body": "Our answer"})
        patch = [c for c in fake.calls if c[0] == "PATCH"][0]
        body = json.loads(patch[3])["body"]
        self.assertEqual(body["contentType"], "HTML")
        self.assertIn("Quoted &lt;one&gt;<br>\nQuoted two", body["content"])
        self.assertLess(
            body["content"].index(SIG_HTML), body["content"].index("Quoted")
        )

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

    def test_from_alone_still_leads_the_filter_with_the_ordered_property(self):
        g, fake = graph_with({"value": []})
        mail.list_rows(g, "inbox", 5, from_who="x@example.test")
        url = fake.calls[0][1]
        self.assertIn("%24orderby=receivedDateTime%20desc", url)
        self.assertIn(
            "filter=receivedDateTime%20ge%201970-01-01T00%3A00%3A00Z%20and%20from", url
        )

    def test_before_filter_orders_ge_then_lt_and_alone_keeps_ge(self):
        g, fake = graph_with({"value": []})
        mail.list_rows(g, "inbox", 5, since="2026-10-01", before="2026-10-31")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][1]).query)
        self.assertEqual(
            query["$filter"],
            [
                "receivedDateTime ge 2026-10-01T00:00:00Z and receivedDateTime lt 2026-10-31T00:00:00Z"
            ],
        )
        g, fake = graph_with({"value": []})
        mail.list_rows(g, "inbox", 5, before="2026-10-31")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][1]).query)
        self.assertEqual(
            query["$filter"],
            [
                "receivedDateTime ge 1970-01-01T00:00:00Z and receivedDateTime lt 2026-10-31T00:00:00Z"
            ],
        )
        self.assertEqual(len(fake.calls), 1)

    def test_search_does_not_combine_with_before(self):
        g, fake = graph_with()
        with self.assertRaises(MailError) as ctx:
            mail.list_rows(g, "inbox", 5, search="invoice", before="2026-10-31")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(fake.calls, [])

    def test_before_cli_validates_date_and_keeps_listing_to_one_page(self):
        with self.assertRaises(SystemExit) as ctx, unittest.mock.patch(
            "sys.stderr", new=io.StringIO()
        ):
            mail.parser().parse_args(["list", "--before", "invented-invalid-date"])
        self.assertEqual(ctx.exception.code, 2)
        args = mail.parser().parse_args(
            ["list", "--before", "2042-03-04", "--top", "5"]
        )
        g, fake = graph_with(
            {
                "value": [],
                "@odata.nextLink": PREFIX + "mailFolders/inbox/messages?$skip=5",
            }
        )
        args.run(g, args, io.StringIO())
        self.assertEqual(len(fake.calls), 1)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][1]).query)
        self.assertEqual(query["$top"], ["5"])
        self.assertEqual(
            query["$filter"],
            [
                "receivedDateTime ge 1970-01-01T00:00:00Z and receivedDateTime lt 2042-03-04T00:00:00Z"
            ],
        )

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


class DraftMutationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sig_dir = signature_dir(self.tmp)
        self.sig_patch = unittest.mock.patch.object(mail, "SIGNATURE_DIR", self.sig_dir)
        self.sig_patch.start()
        self.addCleanup(self.sig_patch.stop)

    def state(self, graph, kind="new"):
        mail.save_drafts(
            [{"id": "draft-x", "kind": kind, "created": "2042-03-04T00:00:00+00:00"}],
            graph.drafts_path,
        )

    def guard_replies(self, parent="folder-drafts", is_draft=True):
        return (
            {"id": "draft-x", "isDraft": is_draft, "parentFolderId": parent},
            {"id": "folder-drafts"},
        )

    def test_update_refuses_non_draft_and_outside_drafts_before_patch(self):
        for parent, is_draft in (("folder-drafts", False), ("other-folder", True)):
            g, fake = graph_with(*self.guard_replies(parent, is_draft))
            self.state(g)
            with self.assertRaises(MailError):
                mail.draft_update(g, "draft-x", {"subject": "Invented"})
            self.assertFalse(fake.sent("PATCH"))

    def test_body_update_composes_signature_and_only_posts_missing_cid(self):
        g, fake = graph_with(
            *self.guard_replies(),
            {"value": [{"contentId": "logo.png@01AB.CD"}]},
            {"id": "draft-x", "webLink": "https://example.test/d"},
        )
        self.state(g)
        link = mail.draft_update(g, "draft-x", {"body": "Invented update"})
        self.assertEqual(link, "https://example.test/d")
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET", "GET", "PATCH"])
        payload = json.loads(fake.sent("PATCH")[0][3])
        self.assertEqual(payload["body"]["contentType"], "HTML")
        self.assertLess(
            payload["body"]["content"].index("Invented update"),
            payload["body"]["content"].index(SIG_HTML),
        )
        self.assertFalse(fake.sent("POST"))

    def test_body_update_avoids_content_id_select_and_posts_only_missing_images(self):
        sig = '<img src="cid:logo.png@01AB.CD"><img src="cid:mark.png@02EF.GH">'
        with open(os.path.join(self.sig_dir, "signature.html"), "w") as f:
            f.write(sig)
        with open(os.path.join(self.sig_dir, "mark.png@02EF.GH"), "wb") as f:
            f.write(b"invented mark")
        fake = GraphAttachmentSelectFake(
            *self.guard_replies(),
            {"value": [{"contentId": "logo.png@01AB.CD"}, {"name": "item"}]},
            {"webLink": "https://example.test/updated"},
            {},
        )
        g = Graph(CFG, b"unused", transport=fake)
        g.token = "token-x"
        g.drafts_path = os.path.join(self.tmp, "mail-drafts.json")
        self.state(g)

        mail.draft_update(g, "draft-x", {"body": "Invented update"})

        attachment_gets = [c for c in fake.sent("GET") if "/attachments" in c[1]]
        self.assertEqual(len(attachment_gets), 1)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(attachment_gets[0][1]).query
        )
        self.assertNotIn("$select", query)
        self.assertEqual(len(fake.sent("PATCH")), 1)
        posts = fake.sent("POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0][3])["contentId"], "mark.png@02EF.GH")

    def test_repeated_body_updates_request_cids_and_do_not_duplicate_images(self):
        g, fake = graph_with(
            *self.guard_replies(),
            {"value": []},
            {},
            {},
            *self.guard_replies(),
            {"value": [{"contentId": "logo.png@01AB.CD"}]},
            {},
        )
        transport = g.transport

        def project_selected_fields(method, url, *args):
            raw = transport(method, url, *args)
            if method == "GET" and "/attachments?" in url:
                selected = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)[
                    "$select"
                ][0].split(",")
                page = json.loads(raw)
                page["value"] = [
                    {k: v for k, v in row.items() if k in selected}
                    for row in page["value"]
                ]
                return json.dumps(page).encode()
            return raw

        g.transport = project_selected_fields
        mail.draft_update(g, "draft-x", {"body": "First invented update"})
        mail.draft_update(g, "draft-x", {"body": "Second invented update"})
        self.assertEqual(len(fake.sent("POST")), 1)
        self.assertEqual(len(fake.sent("PATCH")), 2)

    def test_cid_on_later_attachment_page_is_not_duplicated(self):
        next_page = PREFIX + "messages/draft-x/attachments?$skip=1"
        g, fake = graph_with(
            *self.guard_replies(),
            {"value": [], "@odata.nextLink": next_page},
            {"value": [{"contentId": "logo.png@01AB.CD"}]},
            {},
        )
        mail.draft_update(g, "draft-x", {"body": "Invented update"})
        self.assertEqual(fake.sent("GET")[-1][1], next_page)
        self.assertFalse(fake.sent("POST"))

    def test_failed_patch_does_not_add_images(self):
        g, fake = graph_with(
            *self.guard_replies(), {"value": []}, GraphError("PATCH", 409, "Conflict")
        )
        with self.assertRaises(GraphError):
            mail.draft_update(g, "draft-x", {"body": "Invented update"})
        self.assertFalse(fake.sent("POST"))

    def test_update_command_uses_current_signature_and_prints_draft_link(self):
        g, fake = graph_with(
            *self.guard_replies(),
            {"value": []},
            {"webLink": "https://example.test/updated"},
            {},
        )
        changed_sig = SIG_HTML.replace("Invented Signer", "Invented Current Signer")
        with open(os.path.join(self.sig_dir, "signature.html"), "w") as f:
            f.write(changed_sig)
        out = io.StringIO()
        with unittest.mock.patch.object(
            mail,
            "read_input",
            return_value={
                "subject": "Invented replacement",
                "body": "<p>Invented HTML</p>",
                "contentType": "HTML",
            },
        ):
            args = mail.parser().parse_args(["draft-update", "draft-x"])
            args.run(g, args, out)
        payload = json.loads(fake.sent("PATCH")[0][3])
        self.assertEqual(payload["subject"], "Invented replacement")
        self.assertEqual(
            payload["body"]["content"],
            mail.compose("<p>Invented HTML</p>", changed_sig),
        )
        self.assertEqual(
            out.getvalue(), "draft: draft-x\nwebLink: https://example.test/updated\n"
        )

    def test_subject_only_update_is_allowed_on_tracked_reply(self):
        g, fake = graph_with(*self.guard_replies(), {})
        self.state(g, "reply")
        mail.draft_update(g, "draft-x", {"subject": "Invented replacement"})
        self.assertEqual(
            json.loads(fake.sent("PATCH")[0][3]), {"subject": "Invented replacement"}
        )
        self.assertEqual(len(fake.calls), 3)

    def test_invalid_update_input_refused_before_writes(self):
        for payload in (
            {},
            {"contentType": "HTML"},
            {"subject": None},
            {"body": None},
            {"body": "Invented", "contentType": "invalid"},
        ):
            with self.subTest(payload=payload):
                g, fake = graph_with(*self.guard_replies())
                with self.assertRaises(MailError) as ctx:
                    mail.draft_update(g, "draft-x", payload)
                self.assertEqual(ctx.exception.code, 2)
                self.assertFalse(fake.sent("PATCH"))
                self.assertFalse(fake.sent("POST"))

    def test_body_update_posts_missing_cid(self):
        g, fake = graph_with(
            *self.guard_replies(), {"value": []}, {}, {"id": "draft-x"}
        )
        self.state(g)
        mail.draft_update(g, "draft-x", {"body": "Invented update"})
        self.assertEqual(
            fake.sent("POST")[0][1], PREFIX + "messages/draft-x/attachments"
        )
        self.assertEqual(len(fake.sent("PATCH")), 1)

    def test_subject_only_patch_has_no_body_or_attachment_calls(self):
        g, fake = graph_with(
            *self.guard_replies(), {"webLink": "https://example.test/d"}
        )
        self.state(g)
        mail.draft_update(g, "draft-x", {"subject": "New invented subject"})
        self.assertEqual(
            json.loads(fake.sent("PATCH")[0][3]), {"subject": "New invented subject"}
        )
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET", "PATCH"])

    def test_reply_body_update_refused(self):
        g, fake = graph_with(*self.guard_replies())
        self.state(g, "reply")
        with self.assertRaises(MailError) as ctx:
            mail.draft_update(g, "draft-x", {"body": "Changed"})
        self.assertIn("delete it and make a new draft-reply", str(ctx.exception))
        self.assertFalse(fake.sent("PATCH"))

    def test_delete_requires_tracking_before_guard_and_write(self):
        g, fake = graph_with()
        with self.assertRaises(MailError):
            mail.cmd_draft_delete(g, types.SimpleNamespace(id="draft-x"), io.StringIO())
        self.assertEqual(fake.calls, [])

    def test_delete_refuses_invalid_location(self):
        for parent, is_draft in (("folder-drafts", False), ("other-folder", True)):
            g, fake = graph_with(*self.guard_replies(parent, is_draft))
            self.state(g)
            with self.assertRaises(MailError):
                mail.cmd_draft_delete(
                    g, types.SimpleNamespace(id="draft-x"), io.StringIO()
                )
            self.assertFalse(fake.sent("DELETE"))

    def test_delete_tracked_draft_removes_state(self):
        g, fake = graph_with(*self.guard_replies(), {})
        self.state(g)
        out = io.StringIO()
        mail.cmd_draft_delete(g, types.SimpleNamespace(id="draft-x"), out)
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET", "DELETE"])
        self.assertEqual(fake.sent("DELETE")[0][1], PREFIX + "messages/draft-x")
        self.assertEqual(out.getvalue(), "deleted draft: draft-x\n")
        self.assertEqual(mail.load_drafts(g.drafts_path), [])

    def test_failed_delete_keeps_ownership(self):
        g, _ = graph_with(*self.guard_replies(), GraphError("DELETE", 409, "Conflict"))
        self.state(g)
        with self.assertRaises(GraphError):
            mail.cmd_draft_delete(g, types.SimpleNamespace(id="draft-x"), io.StringIO())
        self.assertEqual(mail.load_drafts(g.drafts_path)[0]["id"], "draft-x")

    def test_delete_keeps_a_draft_recorded_during_the_graph_request(self):
        g, fake = graph_with(*self.guard_replies(), {})
        self.state(g)
        transport = g.transport

        def interleaved(method, *args):
            if method == "DELETE":
                mail.track_draft(g, "another-invented-draft", "reply")
            return transport(method, *args)

        g.transport = interleaved
        mail.cmd_draft_delete(g, types.SimpleNamespace(id="draft-x"), io.StringIO())
        self.assertEqual(
            [d["id"] for d in mail.load_drafts(g.drafts_path)],
            ["another-invented-draft"],
        )
        self.assertEqual(len(fake.sent("DELETE")), 1)

    def test_concurrent_tracking_keeps_every_record(self):
        g, _ = graph_with()
        start = threading.Barrier(8)

        def track(index):
            start.wait(timeout=5)
            mail.track_draft(g, "invented-concurrent-%s" % index, "new")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(track, range(8)))
        self.assertEqual(
            {d["id"] for d in mail.load_drafts(g.drafts_path)},
            {"invented-concurrent-%s" % i for i in range(8)},
        )
        self.assertEqual(stat.S_IMODE(os.stat(g.drafts_path).st_mode), 0o600)

    def test_failed_state_write_keeps_previous_records(self):
        g, _ = graph_with()
        self.state(g)
        with unittest.mock.patch.object(
            mail.json, "dump", side_effect=OSError("invented write failure")
        ):
            with self.assertRaises(OSError):
                mail.track_draft(g, "another-invented-draft", "new")
        self.assertEqual(mail.load_drafts(g.drafts_path)[0]["id"], "draft-x")
        self.assertFalse(
            any(n.endswith(".tmp") for n in os.listdir(os.path.dirname(g.drafts_path)))
        )

    def test_malformed_state_is_refused_without_exposing_contents(self):
        g, fake = graph_with()
        for contents in (
            '{"invented-private-marker"',
            "[null]",
            '[{"id":"invented-private-marker"}]',
        ):
            with self.subTest(contents=contents):
                fd = os.open(
                    g.drafts_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(fd, "w") as f:
                    f.write(contents)
                with self.assertRaises(MailError) as ctx:
                    mail.cmd_draft_delete(
                        g, types.SimpleNamespace(id="draft-x"), io.StringIO()
                    )
                self.assertEqual(ctx.exception.code, 2)
                self.assertNotIn("invented-private-marker", str(ctx.exception))
                self.assertEqual(fake.calls, [])

    def test_state_file_readable_by_others_is_refused(self):
        g, _ = graph_with()
        mail.save_drafts([], g.drafts_path)
        for mode in (0o640, 0o604):
            os.chmod(g.drafts_path, mode)
            with self.assertRaises(MailError):
                mail.load_drafts(g.drafts_path)
            with self.assertRaises(MailError):
                mail.save_drafts([], g.drafts_path)


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
        self.assertEqual(self.fake.calls, [])

    def test_permanent_delete_is_refused_even_for_a_tracked_id(self):
        mail.track_draft(self.g, "m1", "new")
        with self.assertRaises(MailError):
            self.g._send("POST", PREFIX + "messages/m1/permanentDelete", {}, b"{}")
        self.assertEqual(self.fake.calls, [])

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

    def test_read_prints_headers_body_text_and_attachment_sizes(self):
        g, fake = graph_with(
            {
                "subject": "Invented invoice",
                "from": {"emailAddress": {"address": "s@example.test"}},
                "toRecipients": [{"emailAddress": {"address": "p@example.test"}}],
                "ccRecipients": [{"emailAddress": {"address": "c@example.test"}}],
                "receivedDateTime": "2026-10-19T08:30:00Z",
                "hasAttachments": True,
                "body": {"contentType": "html", "content": "<p>Hello &amp; thanks</p>"},
            },
            {
                "value": [
                    {"id": "a1", "name": "bill.pdf", "size": 1234, "isInline": False},
                    {"id": "a2", "name": "logo.png", "size": 56, "isInline": True},
                ]
            },
        )
        out = io.StringIO()
        args = types.SimpleNamespace(id="m1", attachment=None, out=None)
        mail.cmd_read(g, args, out)
        lines = out.getvalue().split("\n")
        self.assertIn("Subject: Invented invoice", lines)
        self.assertIn("Date: 2026-10-19 08:30", lines)
        self.assertIn("From: s@example.test", lines)
        self.assertIn("To: p@example.test", lines)
        self.assertIn("Cc: c@example.test", lines)
        self.assertIn("Hello & thanks", lines)
        self.assertNotIn("<p>", out.getvalue())
        self.assertIn("Attachments:", lines)
        self.assertIn("  bill.pdf  1234 bytes  attached", lines)
        self.assertIn("  logo.png  56 bytes  inline", lines)
        self.assertEqual([c[0] for c in fake.calls], ["GET", "GET"])
        listing_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(fake.sent("GET")[1][1]).query
        )
        self.assertEqual(
            listing_query["$select"], ["id,name,size,contentType,isInline"]
        )

    def test_html_links_cover_order_alt_filtering_entities_and_unclosed_anchor(self):
        body = {
            "contentType": "html",
            "content": '<a href="https://example.test/a?x=1&amp;y=2"> first <b>link</b></a><a href="https://example.test/img"><img alt="Download invoice"></a><a href="mailto:x@example.test">mail</a><a href="#part">fragment</a><a href="HTTP://example.test/end">last',
        }
        self.assertEqual(
            mail.body_links(body),
            [
                {"text": "first link", "href": "https://example.test/a?x=1&y=2"},
                {"text": "Download invoice", "href": "https://example.test/img"},
                {"text": "last", "href": "HTTP://example.test/end"},
            ],
        )

    def test_plain_body_links_have_empty_text(self):
        body = {
            "contentType": "text",
            "content": "See https://example.test/a and https://example.test/b. "
            "(https://example.test/c_(d)). [https://example.test/e]}",
        }
        self.assertEqual(
            mail.body_links(body),
            [
                {"text": "", "href": "https://example.test/a"},
                {"text": "", "href": "https://example.test/b"},
                {"text": "", "href": "https://example.test/c_(d)"},
                {"text": "", "href": "https://example.test/e"},
            ],
        )

    def test_image_link_with_whitespace_uses_alt_but_visible_text_wins(self):
        self.assertEqual(
            mail.body_links(
                {
                    "contentType": "html",
                    "content": '<a href="https://example.test/image"> \n<img alt="Download &amp; view"/>\t</a>'
                    '<a href="https://example.test/text"><img alt="Ignored"/> View <b>invoice</b></a>',
                }
            ),
            [
                {"text": "Download & view", "href": "https://example.test/image"},
                {"text": "View invoice", "href": "https://example.test/text"},
            ],
        )

    def test_nested_anchor_closes_previous_and_forward_keeps_order(self):
        self.assertEqual(
            mail.body_links(
                {
                    "contentType": "html",
                    "content": '<a href="https://example.test/first">first'
                    '<a href="https://example.test/second">second</a>'
                    "<blockquote><p>Invented forwarded message</p>"
                    '<a href="https://example.test/forward">forward</a></blockquote>',
                }
            ),
            [
                {"text": "first", "href": "https://example.test/first"},
                {"text": "second", "href": "https://example.test/second"},
                {"text": "forward", "href": "https://example.test/forward"},
            ],
        )

    def test_href_cleaned_and_entities_decoded_only_once(self):
        self.assertEqual(
            mail.body_links(
                {
                    "contentType": "html",
                    "content": '<a href=" \tHtTpS://example.test/\x00x\x7f?x=1&amp;amp;y=2\n">go</a>'
                    '<a href="tel:123">tel</a><a href="cid:invented">cid</a>'
                    '<a href="">empty</a><a>missing</a>',
                }
            ),
            [{"text": "go", "href": "HtTpS://example.test/x?x=1&amp;y=2"}],
        )

    def test_links_read_is_one_get_and_does_not_fetch_attachments(self):
        g, fake = graph_with(
            {
                "body": {
                    "contentType": "html",
                    "content": '<p>forward</p><a href="https://example.test/x">invoice</a>',
                }
            }
        )
        out = io.StringIO()
        mail.cmd_read(
            g,
            types.SimpleNamespace(
                id="m1", attachment=None, out=None, links=True, json=False
            ),
            out,
        )
        self.assertEqual(out.getvalue(), "invoice\thttps://example.test/x\n")
        self.assertEqual([call[0] for call in fake.calls], ["GET"])
        self.assertIn(
            "%24select=" + urllib.parse.quote(mail.READ_SELECT, safe=""),
            fake.calls[0][1],
        )

    def test_json_read_has_fixed_keys_and_paginated_attachment_metadata(self):
        msg = {
            "from": {"emailAddress": {"address": "from@example.test"}},
            "toRecipients": [],
            "ccRecipients": [],
            "subject": "Hi",
            "receivedDateTime": "2026-10-01T00:00:00Z",
            "body": {
                "contentType": "html",
                "content": '<p>Hello</p><a href="https://example.test/x">go</a><p>Attachments:</p>',
            },
        }
        next_url = (
            PREFIX
            + "messages/m1/attachments?%24skip=1&%24select=id%2Cname%2Csize%2CcontentType%2CisInline"
        )
        g, fake = graph_with(
            msg,
            {
                "value": [
                    {
                        "name": "one.pdf",
                        "size": 10,
                        "contentType": "application/pdf",
                        "isInline": False,
                    }
                ],
                "@odata.nextLink": next_url,
            },
            {
                "value": [
                    {
                        "name": "two.pdf",
                        "size": 20,
                        "contentType": "application/pdf",
                        "isInline": False,
                    }
                ]
            },
        )
        out = io.StringIO()
        mail.cmd_read(
            g,
            types.SimpleNamespace(
                id="m1", attachment=None, out=None, links=False, json=True
            ),
            out,
        )
        result = json.loads(out.getvalue())
        self.assertEqual(
            set(result),
            {"from", "to", "cc", "received", "subject", "text", "links", "attachments"},
        )
        self.assertEqual(result["text"], "Hello\ngo\nAttachments:")
        self.assertEqual(result["from"], "from@example.test")
        self.assertEqual(result["to"], [])
        self.assertEqual(result["cc"], [])
        self.assertEqual(result["subject"], msg["subject"])
        self.assertEqual(result["received"], msg["receivedDateTime"])
        self.assertEqual(
            result["links"], [{"text": "go", "href": "https://example.test/x"}]
        )
        self.assertEqual(
            result["attachments"],
            [
                {
                    "name": "one.pdf",
                    "size": 10,
                    "contentType": "application/pdf",
                    "isInline": False,
                },
                {
                    "name": "two.pdf",
                    "size": 20,
                    "contentType": "application/pdf",
                    "isInline": False,
                },
            ],
        )
        self.assertEqual(out.getvalue(), json.dumps(result, sort_keys=True) + "\n")
        self.assertEqual(len(result["attachments"]), 2)
        self.assertEqual(len(fake.calls), 3)
        for call in fake.calls[1:]:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(call[1]).query)
            self.assertEqual(query["$select"], ["id,name,size,contentType,isInline"])
            self.assertNotIn("contentBytes", query["$select"][0])

    def test_read_modes_exclude_each_other_and_plain_output_is_unchanged(self):
        for argv in (
            ["read", "m1", "--links", "--json"],
            ["read", "m1", "--links", "--attachment", "x", "--out", "/tmp"],
            ["read", "m1", "--json", "--attachment", "x", "--out", "/tmp"],
        ):
            with self.assertRaises(SystemExit) as ctx, unittest.mock.patch(
                "sys.stderr", new=io.StringIO()
            ):
                mail.parser().parse_args(argv)
            self.assertEqual(ctx.exception.code, 2)
        g, _ = graph_with({"body": {"contentType": "text", "content": "Plain body"}})
        out = io.StringIO()
        mail.cmd_read(g, types.SimpleNamespace(id="m1", attachment=None, out=None), out)
        self.assertEqual(
            out.getvalue(), "From: \nTo: \nDate: \nSubject: \n\nPlain body\n"
        )

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

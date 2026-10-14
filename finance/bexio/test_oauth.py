"""Offline tests for oauth.py: login exchange, refresh with rotation, refusals, scopes, the launchers. No network, no av.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import os
import types
import unittest
import urllib.error
from unittest import mock

import launcher

os.environ["BEXIO_DIR"] = os.path.dirname(os.path.abspath(__file__))  # run this checkout's files, never the main checkout's
oauth = launcher.load("oauth.py")  # the launcher is a bash file; the tests run its python body
import private_guard
OAuthError = oauth.OAuthError
VaultError = oauth.VaultError

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_ENV = {"BEXIO_CLIENT_ID": "client-id-not-real", "BEXIO_CLIENT_SECRET": "secret-not-real"}
SAVE_ARGV = [
    "/usr/local/bin/av", "save", "--stdin", "--project-directory", "/Users/bsaladin/ws_yardr_finance",
    "BEXIO_REFRESH_TOKEN",
]


def setUpModule():
    private_guard.setUpModule()  # every test below runs against a temp private folder, never the real one
    # oauth's subprocess is replaced for the whole module: a save that no test mocks fails here instead of writing the real vault.
    guard = mock.patch.object(oauth, "subprocess")
    guard.start().run.side_effect = AssertionError("a test reached av")
    unittest.addModuleCleanup(guard.stop)


class ScopeTest(unittest.TestCase):
    def test_scope_has_no_write_scope(self):
        # Every scope is an identity scope or a *_show (read). A write scope
        # such as `accounting` or a *_edit would let the login change the account.
        for scope in oauth.SCOPE.split():
            self.assertTrue(
                scope in ("openid", "offline_access") or scope.endswith("_show"),
                "unexpected scope {!r}".format(scope),
            )
        self.assertNotIn("accounting", oauth.SCOPE.split())
        self.assertFalse(any(s.endswith("_edit") for s in oauth.SCOPE.split()))

    def test_scope_has_the_read_scopes_the_bead_asks_for(self):
        self.assertEqual(
            oauth.SCOPE.split(),
            "openid offline_access contact_show note_show article_show kb_invoice_show "
            "kb_offer_show kb_order_show kb_delivery_show kb_bill_show kb_expense_show "
            "bank_account_show bank_payment_show payroll_employee_show payroll_absence_show "
            "payroll_paystub_show".split(),
        )


class ExportScopeTest(unittest.TestCase):
    def test_export_scope_is_the_read_scopes_plus_accounting_and_file(self):
        self.assertEqual(
            set(oauth.EXPORT_SCOPE.split()),
            set(oauth.SCOPE.split()) | {"accounting", "file"},
        )
        self.assertFalse(any(s.endswith("_edit") for s in oauth.EXPORT_SCOPE.split()))

    def test_export_login_has_its_own_vault_value(self):
        self.assertNotEqual(oauth.EXPORT_REFRESH_KEY, oauth.REFRESH_KEY)

    def test_export_login_asks_for_the_export_scope_and_saves_to_its_own_value(self):
        self._login(export_scope=True)
        self.authorize.assert_called_once()
        self.assertEqual(self.authorize.call_args.args[2], oauth.EXPORT_SCOPE)
        self.save.assert_called_once_with("refresh-not-real", oauth.EXPORT_REFRESH_KEY)

    def test_default_login_stays_read_only_and_saves_to_the_read_value(self):
        self._login(export_scope=False)
        self.assertEqual(self.authorize.call_args.args[2], oauth.SCOPE)
        self.save.assert_called_once_with("refresh-not-real", oauth.REFRESH_KEY)

    def _login(self, export_scope):
        body = {"access_token": "access-not-real", "refresh_token": "refresh-not-real"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "authorize_url", return_value="https://example.invalid/") as self.authorize, \
                mock.patch.object(oauth.webbrowser, "open"), \
                mock.patch.object(oauth, "wait_for_code", return_value="code-not-real"), \
                mock.patch.object(oauth, "exchange_code", return_value=body), \
                mock.patch.object(oauth, "save_refresh_token") as self.save, \
                contextlib.redirect_stderr(io.StringIO()):
            oauth.login(export_scope=export_scope)


class LogoutTest(unittest.TestCase):
    def test_logout_cannot_remove_the_vault_value_and_says_so(self):
        # av has no delete, so logout names the value to remove by hand and touches nothing itself.
        with mock.patch.object(oauth, "subprocess") as sub:
            with self.assertRaises(SystemExit) as ctx:
                oauth.main(["logout", "--export-scope"])
        self.assertIn("BEXIO_EXPORT_REFRESH_TOKEN", str(ctx.exception))
        sub.run.assert_not_called()

    def test_logout_without_export_scope_is_refused(self):
        with mock.patch.object(oauth, "subprocess") as sub:
            with self.assertRaises(SystemExit) as ctx:
                oauth.main(["logout"])
        self.assertEqual(str(ctx.exception), oauth.USAGE)
        sub.run.assert_not_called()

    def test_login_flag_selects_the_export_login(self):
        with mock.patch.object(oauth, "login") as login:
            oauth.main(["login", "--export-scope"])
        login.assert_called_once_with(True)


class PkceTest(unittest.TestCase):
    def test_challenge_matches_the_rfc_7636_example(self):
        # RFC 7636, appendix B: the verifier and challenge pair.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(oauth._challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


class AuthorizeUrlTest(unittest.TestCase):
    def test_url_asks_for_the_read_scopes_with_state_and_pkce(self):
        with mock.patch.dict(os.environ, FAKE_ENV):
            url = oauth.authorize_url("state-value", "challenge-value")
        self.assertTrue(url.startswith(oauth.AUTHORIZE_URL + "?"))
        query = dict(q.split("=", 1) for q in url.split("?", 1)[1].split("&"))
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["state"], "state-value")
        self.assertEqual(query["code_challenge"], "challenge-value")
        self.assertIn("client_id", query)
        self.assertNotIn("client_secret", url)
        self.assertNotIn("secret-not-real", url)


class LoginExchangeTest(unittest.TestCase):
    def test_code_is_exchanged_with_the_verifier_and_the_secret(self):
        body = {"access_token": "access-not-real", "refresh_token": "refresh-not-real"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "_token_request", return_value=body) as request:
            self.assertEqual(oauth.exchange_code("code-not-real", "verifier-not-real"), body)
        fields = request.call_args.args[0]
        self.assertEqual(fields["grant_type"], "authorization_code")
        self.assertEqual(fields["code"], "code-not-real")
        self.assertEqual(fields["code_verifier"], "verifier-not-real")
        self.assertEqual(fields["redirect_uri"], oauth.REDIRECT_URI)
        self.assertEqual(fields["client_secret"], "secret-not-real")


class ReadTokenTest(unittest.TestCase):
    def test_refresh_token_is_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {oauth.REFRESH_KEY: "refresh-from-env"}):
            self.assertEqual(oauth.read_refresh_token(), "refresh-from-env")
            self.assertEqual(oauth.read_refresh_token(oauth.REFRESH_KEY), "refresh-from-env")

    def test_unset_or_empty_refresh_token_is_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(oauth.read_refresh_token())
        with mock.patch.dict(os.environ, {oauth.EXPORT_REFRESH_KEY: ""}, clear=True):
            self.assertIsNone(oauth.read_refresh_token(oauth.EXPORT_REFRESH_KEY))


class RefreshTest(unittest.TestCase):
    def test_refresh_returns_access_token_and_saves_the_rotated_refresh_token(self):
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value=answer) as request, \
                mock.patch.object(oauth, "save_refresh_token") as save:
            self.assertEqual(oauth.refresh_access_token(), "access-new")
        fields = request.call_args.args[0]
        self.assertEqual(fields["grant_type"], "refresh_token")
        self.assertEqual(fields["refresh_token"], "refresh-old")
        save.assert_called_once_with("refresh-rotated", oauth.REFRESH_KEY)

    def test_rotated_token_goes_to_av_on_stdin_and_never_on_argv(self):
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value=answer), \
                mock.patch.object(oauth, "subprocess") as sub:
            sub.run.return_value = types.SimpleNamespace(returncode=0)
            self.assertEqual(oauth.refresh_access_token(), "access-new")
        sub.run.assert_called_once()
        self.assertEqual(sub.run.call_args.args[0], SAVE_ARGV)
        self.assertEqual(sub.run.call_args.kwargs["input"], b"refresh-rotated")
        self.assertTrue(sub.run.call_args.kwargs["capture_output"])  # av's output is dropped, never shown
        self.assertNotIn("refresh-rotated", " ".join(sub.run.call_args.args[0]))

    def test_the_rotated_token_is_the_one_the_rest_of_the_run_reads(self):
        # bexio spent the old token, so a second Client in the same run must read the new one.
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, dict(FAKE_ENV, **{oauth.REFRESH_KEY: "refresh-old"})), \
                mock.patch.object(oauth, "_token_request", return_value=answer), \
                mock.patch.object(oauth, "subprocess") as sub:
            sub.run.return_value = types.SimpleNamespace(returncode=0)
            oauth.refresh_access_token()
            self.assertEqual(oauth.read_refresh_token(), "refresh-rotated")

    def test_failed_save_stops_the_refresh_and_hands_out_no_access_token(self):
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value=answer), \
                mock.patch.object(oauth, "subprocess") as sub:
            sub.run.return_value = types.SimpleNamespace(returncode=1)
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        message = str(ctx.exception)
        self.assertIn("could not be saved", message)
        self.assertIn("a new login is needed", message)
        self.assertIn("login_helper.py", message)
        self.assertNotIn("refresh-rotated", message)
        self.assertNotIn("access-new", message)

    def test_av_that_cannot_run_stops_the_refresh_too(self):
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value=answer), \
                mock.patch.object(oauth, "subprocess") as sub:
            sub.run.side_effect = FileNotFoundError(2, "No such file or directory")
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        self.assertIn("login_helper.py", str(ctx.exception))
        self.assertNotIn("refresh-rotated", str(ctx.exception))

    def test_export_refresh_uses_its_own_vault_value(self):
        answer = {"access_token": "access-export", "refresh_token": "refresh-export-rotated"}
        with mock.patch.dict(os.environ, dict(FAKE_ENV, **{oauth.EXPORT_REFRESH_KEY: "refresh-export-old"})), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-export-old") as read, \
                mock.patch.object(oauth, "_token_request", return_value=answer), \
                mock.patch.object(oauth, "save_refresh_token") as save:
            self.assertEqual(oauth.refresh_access_token(export_scope=True), "access-export")
        read.assert_called_once_with(oauth.EXPORT_REFRESH_KEY)
        save.assert_called_once_with("refresh-export-rotated", oauth.EXPORT_REFRESH_KEY)

    def test_refused_refresh_token_asks_for_a_new_login(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", side_effect=OAuthError("bexio answered HTTP 400 (invalid_grant)")), \
                mock.patch.object(oauth, "save_refresh_token") as save:
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertIn("oauth.py login", str(ctx.exception))
        self.assertNotIn("refresh-old", str(ctx.exception))
        save.assert_not_called()

    def test_missing_refresh_token_asks_for_a_login(self):
        with mock.patch.object(oauth, "read_refresh_token", return_value=None), \
                mock.patch.object(oauth, "_token_request") as request:
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        self.assertIn("oauth.py login", str(ctx.exception))
        request.assert_not_called()

    def test_answer_without_tokens_is_an_error(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value={"access_token": "a"}), \
                mock.patch.object(oauth, "save_refresh_token") as save:
            with self.assertRaises(OAuthError):
                oauth.refresh_access_token()
        save.assert_not_called()


class TokenRequestTest(unittest.TestCase):
    def _http_error(self, code, body):
        return urllib.error.HTTPError(oauth.TOKEN_URL, code, "err", {}, io.BytesIO(body))

    def test_refusal_carries_bexio_error_code_only(self):
        err = self._http_error(400, b'{"error":"invalid_grant","error_description":"refresh-old is not active"}')
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OAuthError) as ctx:
                oauth._token_request({"grant_type": "refresh_token", "refresh_token": "refresh-old"})
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertNotIn("refresh-old", str(ctx.exception))

    def test_refusal_with_a_body_that_is_not_json(self):
        err = self._http_error(502, b"<html>bad gateway</html>")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OAuthError) as ctx:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("HTTP 502", str(ctx.exception))

    def test_network_failure_is_not_called_a_refusal(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(OAuthError) as ctx:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("could not reach", str(ctx.exception))


class CallbackHandlerTest(unittest.TestCase):
    """The handler's decisions, without a socket: a fake server and request."""

    def _run(self, path, expected_state="state-ok"):
        handler = oauth._CallbackHandler.__new__(oauth._CallbackHandler)
        handler.path = path
        handler.server = mock.Mock(expected_state=expected_state, outcome=None)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        handler.do_GET()
        return handler.server.outcome, handler

    def test_code_with_the_right_state_is_taken(self):
        outcome, _ = self._run("/callback?code=code-not-real&state=state-ok")
        self.assertEqual(outcome, {"code": "code-not-real"})

    def test_state_mismatch_ends_the_login(self):
        outcome, _ = self._run("/callback?code=code-not-real&state=other", expected_state="state-ok")
        self.assertIn("error", outcome)
        self.assertNotIn("code", outcome)

    def test_non_ascii_state_is_a_mismatch_not_a_crash(self):
        outcome, _ = self._run("/callback?code=code-not-real&state=%C3%A9")
        self.assertIn("error", outcome)

    def test_bexio_error_is_reported(self):
        outcome, _ = self._run("/callback?error=access_denied&state=state-ok")
        self.assertEqual(outcome, {"error": "access_denied"})

    def test_other_paths_are_404_and_do_not_end_the_wait(self):
        outcome, handler = self._run("/favicon.ico")
        self.assertIsNone(outcome)
        handler.send_error.assert_called_once_with(404)

    def test_requests_are_not_logged(self):
        # The default log prints the request line, which holds the code.
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            handler = oauth._CallbackHandler.__new__(oauth._CallbackHandler)
            handler.log_message("%s", "GET /callback?code=code-not-real")
        self.assertEqual(err.getvalue(), "")


class LauncherTest(unittest.TestCase):
    """The launchers are blessed by hash, so what they run with is pinned here: the shebang, the mode, no keychain."""

    SHELL = "/bin/bash"
    LAUNCHERS = {
        "oauth.py": "+BEXIO_CLIENT_ID +BEXIO_CLIENT_SECRET",
        "login_helper.py": "+BEXIO_CLIENT_ID +BEXIO_CLIENT_SECRET",
        "inventory.py": "+BEXIO_CLIENT_ID +BEXIO_CLIENT_SECRET +BEXIO_REFRESH_TOKEN +BEXIO_READ_TOKEN",
        "export.py": "+BEXIO_CLIENT_ID +BEXIO_CLIENT_SECRET +BEXIO_REFRESH_TOKEN +BEXIO_EXPORT_REFRESH_TOKEN +BEXIO_READ_TOKEN",
    }
    # The retired read path's two names, put together from parts: the depot gate greps
    # the repo for them and must find none, so this file cannot spell them either. The
    # keychain is not in that grep and stays spelled out.
    RETIRED = ("".join(("var", "lock")), "".join(("bro", "ker")))
    TEXTS = list(LAUNCHERS) + ["client.py", "ch.bi-concepts.bexio-login.plist"]

    def test_the_first_line_is_the_av_inject_shebang_for_bash(self):
        for name, keys in self.LAUNCHERS.items():
            with open(os.path.join(HERE, name)) as f:
                first = f.readline().rstrip("\n")
            self.assertEqual(first, "#!/usr/local/bin/av inject {} {}".format(keys, self.SHELL), name)

    def test_the_launchers_are_executable(self):
        for name in self.LAUNCHERS:
            self.assertTrue(os.access(os.path.join(HERE, name), os.X_OK), "chmod +x {}".format(name))

    def test_no_keychain_or_retired_name_is_left(self):
        for name in self.TEXTS:
            with open(os.path.join(HERE, name)) as f:
                stray = [line for line in f
                         if any(n in line.lower() for n in self.RETIRED + ("keychain",))]
            self.assertEqual(stray, [], name)


if __name__ == "__main__":
    unittest.main()

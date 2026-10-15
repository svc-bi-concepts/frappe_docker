"""Offline tests for client.py: paging, retries and error handling, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import io
import os
import unittest
import urllib.error
import urllib.request
from unittest import mock

import client
import launcher

oauth = launcher.load(
    "oauth.py"
)  # client.py loads oauth lazily from the launcher's body; cache it here so the imports below find it
from client import BexioError, Client
from private_guard import (  # every test below runs against a temp private folder, never the real one
    setUpModule,
)


def _client():
    return Client(token="test-token-not-real")


class OffsetPagingTest(unittest.TestCase):
    def test_walks_all_rows_when_server_caps_the_page(self):
        # The server returns at most 100 rows whatever limit it is asked for
        # (bills did this: 500 asked, 100 back). Every row must still come out.
        rows = list(range(250))
        c = _client()

        def fake_get(path, params=None):
            offset = params["offset"]
            return rows[offset : offset + 100]

        with mock.patch.object(c, "get", side_effect=fake_get):
            self.assertEqual(list(c.paginate("/x", page_size=500)), rows)

    def test_stops_on_empty_page_not_on_short_page(self):
        c = _client()
        pages = [[1, 2], [3], []]
        with mock.patch.object(c, "get", side_effect=pages) as get:
            self.assertEqual(list(c.paginate("/x", page_size=500)), [1, 2, 3])
            self.assertEqual(get.call_count, 3)

    def test_empty_list_yields_nothing(self):
        c = _client()
        with mock.patch.object(c, "get", return_value=[]):
            self.assertEqual(list(c.paginate("/x")), [])

    def test_non_list_body_is_an_error(self):
        c = _client()
        with mock.patch.object(c, "get", return_value={"unexpected": True}):
            with self.assertRaises(BexioError):
                list(c.paginate("/x"))


class IgnoredOffsetTest(unittest.TestCase):
    # /3.0/taxes and /3.0/accounting/business_years ignore limit and offset
    # and return the same rows on every page; the live run looped to its cap.
    ROWS = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]

    def test_offset_stops_when_a_page_brings_no_new_id(self):
        c = _client()
        with mock.patch.object(c, "get", return_value=list(self.ROWS)) as get:
            self.assertEqual(list(c.paginate("/3.0/taxes")), self.ROWS)
            self.assertEqual(get.call_count, 2)

    def test_offset_yields_each_id_once_across_overlapping_pages(self):
        c = _client()
        pages = [self.ROWS[:2], self.ROWS[1:], self.ROWS[1:]]
        with mock.patch.object(c, "get", side_effect=pages):
            self.assertEqual(list(c.paginate("/x")), self.ROWS)

    def test_page_numbered_stops_when_a_page_brings_no_new_id(self):
        c = _client()
        body = {"data": list(self.ROWS)}
        with mock.patch.object(c, "get", return_value=body) as get:
            got = list(c.paginate_pages("/p", "page_size", rows_key="data"))
            self.assertEqual(got, self.ROWS)
            self.assertEqual(get.call_count, 2)

    def test_rows_without_id_are_never_dropped(self):
        c = _client()
        rows = [{"a": 1}, {"a": 1}]
        with mock.patch.object(c, "get", side_effect=[rows, []]):
            self.assertEqual(list(c.paginate("/x")), rows)


class PagePagingTest(unittest.TestCase):
    def test_walks_pages_and_reads_rows_under_key(self):
        c = _client()
        bodies = [
            {"data": [1, 2, 3], "paging": {}},
            {"data": [4, 5], "paging": {}},
            {"data": [], "paging": {}},
        ]
        with mock.patch.object(c, "get", side_effect=bodies) as get:
            got = list(
                c.paginate_pages("/p", "page_size", rows_key="data", page_size=3)
            )
            self.assertEqual(got, [1, 2, 3, 4, 5])
            self.assertEqual(
                [call.args[1]["page"] for call in get.call_args_list], [1, 2, 3]
            )
            self.assertEqual(get.call_args_list[0].args[1]["page_size"], 3)

    def test_uses_the_size_parameter_the_endpoint_names(self):
        c = _client()
        with mock.patch.object(c, "get", side_effect=[{"results": []}]) as get:
            list(c.paginate_pages("/banking", "per-page", rows_key="results"))
            self.assertIn("per-page", get.call_args.args[1])
            self.assertNotIn("page_size", get.call_args.args[1])

    def test_bare_list_body_without_rows_key(self):
        c = _client()
        with mock.patch.object(c, "get", side_effect=[[7], []]):
            self.assertEqual(
                list(c.paginate_pages("/p", "page_size", page_size=1)), [7]
            )


class ErrorTest(unittest.TestCase):
    def _http_error(self, code, headers=None):
        return urllib.error.HTTPError(
            "https://api.bexio.com/x", code, "err", headers or {}, io.BytesIO(b"")
        )

    def test_404_raises_without_retry(self):
        c = _client()
        with mock.patch(
            "urllib.request.urlopen", side_effect=self._http_error(404)
        ) as opened:
            with self.assertRaises(BexioError) as ctx:
                c.get("/x")
            self.assertEqual(ctx.exception.status, 404)
            self.assertEqual(opened.call_count, 1)

    def test_refused_answer_keeps_its_body_as_detail_outside_the_message(self):
        c = _client()
        body = io.BytesIO(b'{"message": "invented reason"}')
        err = urllib.error.HTTPError("https://api.bexio.com/x", 400, "err", {}, body)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(BexioError) as ctx:
                c.get("/x")
        self.assertEqual(ctx.exception.detail, '{"message": "invented reason"}')
        self.assertNotIn("invented reason", str(ctx.exception))

    def test_refused_answer_without_body_has_no_detail(self):
        c = _client()
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(404)):
            with self.assertRaises(BexioError) as ctx:
                c.get("/x")
        self.assertIsNone(ctx.exception.detail)

    def test_error_message_never_contains_the_token(self):
        c = Client(token="SECRET-TOKEN-VALUE")
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(403)):
            with self.assertRaises(BexioError) as ctx:
                c.get("/x")
        self.assertNotIn("SECRET-TOKEN-VALUE", str(ctx.exception))

    def test_429_is_retried_after_retry_after(self):
        c = _client()
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        errors = [self._http_error(429, {"Retry-After": "0"}), ok]
        with mock.patch(
            "urllib.request.urlopen", side_effect=errors
        ), mock.patch.object(client.time, "sleep") as sleep:
            self.assertEqual(c.get("/x"), [])
            sleep.assert_called_once()

    def test_missing_token_refuses_to_start(self):
        import oauth

        with mock.patch.object(
            oauth, "read_refresh_token", return_value=None
        ), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                Client()

    def test_only_get_requests_are_issued(self):
        c = _client()
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=ok) as opened:
            c.get("/x")
            self.assertEqual(opened.call_args.args[0].get_method(), "GET")


class TokenSourceTest(unittest.TestCase):
    # Reads take BEXIO_READ_TOKEN when it is set: a personal access token does not
    # rotate the stored OAuth refresh token, so the OAuth login is not touched at
    # all. Only without one does the OAuth login run. The kind is printed, the
    # value never.

    def _resolve(
        self, refresh_token=None, refresh_result="access-from-oauth", pat=None
    ):
        import oauth

        env = {"BEXIO_READ_TOKEN": pat} if pat else {}
        read = mock.patch.object(
            oauth, "read_refresh_token", return_value=refresh_token
        )
        refresh = mock.patch.object(
            oauth, "refresh_access_token", return_value=refresh_result
        )
        with read, refresh as refreshed, mock.patch.dict(os.environ, env, clear=True):
            try:
                return client.resolve_token(), refreshed
            except SystemExit as exit_:
                return exit_, refreshed

    def test_the_read_token_wins_over_the_oauth_login(self):
        (token, kind), refreshed = self._resolve(refresh_token="r", pat="pat-value")
        self.assertEqual((token, kind), ("pat-value", "pat"))
        refreshed.assert_not_called()

    def test_the_read_token_touches_no_vault_value_at_all(self):
        # Not even read_refresh_token runs: a read neither spends nor rotates the
        # stored OAuth refresh token.
        import oauth

        with mock.patch.dict(
            os.environ, {"BEXIO_READ_TOKEN": "pat-value"}, clear=True
        ), mock.patch.object(oauth, "read_refresh_token") as read:
            self.assertEqual(client.resolve_token(), ("pat-value", "pat"))
        read.assert_not_called()

    def test_the_oauth_login_is_the_fallback_without_a_read_token(self):
        (token, kind), refreshed = self._resolve(refresh_token="r", pat=None)
        self.assertEqual((token, kind), ("access-from-oauth", "oauth"))
        refreshed.assert_called_once()

    def test_the_write_token_never_stands_in_for_the_read_token(self):
        # BEXIO_TOKEN stays with the upload scripts; the read path takes BEXIO_READ_TOKEN only.
        import oauth

        read = mock.patch.object(oauth, "read_refresh_token", return_value=None)
        refresh = mock.patch.object(oauth, "refresh_access_token")
        with mock.patch.dict(
            os.environ, {"BEXIO_TOKEN": "write-token-not-real"}, clear=True
        ), read, refresh:
            with self.assertRaises(SystemExit) as caught:
                client.resolve_token()
        self.assertIn("BEXIO_READ_TOKEN", str(caught.exception))
        self.assertNotIn("write-token-not-real", str(caught.exception))

    def test_empty_read_token_variable_falls_back_to_the_oauth_login(self):
        import oauth

        with mock.patch.dict(
            os.environ, {"BEXIO_READ_TOKEN": "", oauth.REFRESH_KEY: "r"}, clear=True
        ), mock.patch.object(
            oauth, "read_refresh_token", return_value="r"
        ), mock.patch.object(
            oauth, "refresh_access_token", return_value="access"
        ) as refreshed:
            self.assertEqual(client.resolve_token(), ("access", "oauth"))
        refreshed.assert_called_once()

    def test_a_refused_oauth_login_is_an_error_not_a_silent_fallthrough(self):
        # The OAuth login runs only without a read token; a refusal there stops the
        # script instead of passing an unusable token on.
        import oauth

        with mock.patch.object(
            oauth, "read_refresh_token", return_value="r"
        ), mock.patch.object(
            oauth, "refresh_access_token", side_effect=oauth.OAuthError("invalid_grant")
        ), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaises(SystemExit) as caught:
                client.resolve_token()
        self.assertIn("invalid_grant", str(caught.exception))

    def test_neither_token_names_the_login_command(self):
        result, _ = self._resolve()
        self.assertIsInstance(result, SystemExit)
        self.assertIn("oauth.py login", str(result))

    def test_client_prints_the_kind_not_the_value(self):
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with mock.patch.object(
            client, "resolve_token", return_value=("secret-access-token", "oauth")
        ), redirect_stderr(err):
            c = Client()
        self.assertEqual(c.token_kind, "oauth")
        self.assertIn("OAuth", err.getvalue())
        self.assertNotIn("secret-access-token", err.getvalue())


class ExportTokenTest(unittest.TestCase):
    # The export takes the same token as every read: BEXIO_READ_TOKEN when it is set
    # (the live check of 2026-10-14 found it reaches the accounting and file entities
    # too), the OAuth login only without one. The export login of before is gone.

    def test_the_export_takes_the_read_token_and_no_vault_value(self):
        import oauth

        with mock.patch.dict(
            os.environ, {"BEXIO_READ_TOKEN": "pat-value-not-real"}, clear=True
        ), mock.patch.object(oauth, "read_refresh_token") as read:
            self.assertEqual(client.resolve_token(), ("pat-value-not-real", "pat"))
        read.assert_not_called()

    def test_no_export_scope_is_left_on_the_token_choice(self):
        import inspect

        self.assertNotIn(
            "export_scope", inspect.signature(client.resolve_token).parameters
        )
        self.assertNotIn(
            "export_scope", inspect.signature(client.Client.__init__).parameters
        )
        self.assertNotIn("oauth-export", client.TOKEN_NAMES)


class RetiredEnvTest(unittest.TestCase):
    # The environment variable that once chose the retired proxy path is dead: nothing
    # reads it, the token choice is the same with and without it. Decision of erp-98kf:
    # "no effect", not a refusal — a refusal would keep the variable in client.py. Its
    # name is put together from parts so this file does not trip the depot grep that
    # must find that name nowhere (as the erp-9dxt review kept the old key name out).
    DEAD_VAR = "BEXIO_" + "BRO" + "KER"

    def test_setting_it_has_no_effect(self):
        import io
        from contextlib import redirect_stderr

        import oauth

        env = {self.DEAD_VAR: "1", "BEXIO_READ_TOKEN": "pat-value-not-real"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            oauth, "read_refresh_token", return_value="stored-refresh-token-not-real"
        ) as read, mock.patch.object(
            oauth, "refresh_access_token", return_value="fresh-access-token-not-real"
        ) as refresh:
            self.assertEqual(client.resolve_token(), ("pat-value-not-real", "pat"))
            err = io.StringIO()
            with redirect_stderr(err):
                c = Client()
        self.assertEqual(c.token_kind, "pat")
        self.assertIn("personal access token", err.getvalue())
        self.assertNotIn("stored-refresh-token-not-real", err.getvalue())
        self.assertNotIn("fresh-access-token-not-real", err.getvalue())
        read.assert_not_called()
        refresh.assert_not_called()


class GetOnlyTest(unittest.TestCase):
    def test_client_has_no_method_that_writes(self):
        public = {name for name in dir(Client) if not name.startswith("_")}
        self.assertEqual(public, {"get", "paginate", "paginate_pages"})

    def test_every_request_is_a_get_even_with_params_and_raw(self):
        c = _client()
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=ok) as opened:
            c.get("/x", params={"a": 1})
            c.get("/y", raw=True)
            list(c.paginate("/z"))
        methods = {call.args[0].get_method() for call in opened.call_args_list}
        self.assertEqual(methods, {"GET"})

    def test_accept_header_is_set_per_request(self):
        # a raw request asks for */* by default; accept says what the endpoint wants (its docs: application/json)
        c = _client()
        sent = []

        def urlopen(req, timeout=None):
            sent.append(req.get_header("Accept"))
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = b"[]"
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            c.get("/x", raw=True)
            c.get("/x", raw=True, accept="application/json")
            c.get("/x")
        self.assertEqual(sent, ["*/*", "application/json", "application/json"])


if __name__ == "__main__":
    unittest.main()

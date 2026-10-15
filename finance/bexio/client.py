"""Read-only client for the bexio REST API.

GET only: this module has no way to write to bexio, so the scripts cannot
change the account. Reads take the BEXIO_READ_TOKEN environment variable
first, the read-scoped personal access token the read launchers inject from
the Automic Vault: a personal access token does not rotate anything, so a
read never spends or re-saves the stored OAuth refresh token. Only where no
read token is set does the OAuth refresh token in the environment run
(oauth.refresh_access_token()). The export takes the same token as every
read: the live check of 2026-10-14 (inventory.py with only the read token,
against the OAuth report of the same day) found the read token reaches the
accounting and file entities too, so the export login of before is gone.
The write-capable BEXIO_TOKEN of the upload scripts is never read here: the
read-only path takes its credential, not the write one. Which of these was
used is printed to stderr; the token itself is never stored, printed or put
in an error message.

The scripts that use it are launchers (their first line is the av shebang), so
they run as:
    finance/bexio/inventory.py

Standard library only, so it runs on the system Python.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.bexio.com"

# bexio caps a page at 2000 records; a smaller page costs more requests.
PAGE_SIZE = 500

# Retries for 429 (rate limit) and 5xx answers before giving up.
MAX_RETRIES = 5


class BexioError(Exception):
    """A bexio answer that is not a usable 2xx response.

    detail is the start of the refused answer's body, when there was one: bexio says why in it
    (a parameter it wants, a scope it lacks). It is kept apart from the message, which goes to stderr.
    """

    def __init__(self, status, path, detail=None):
        super().__init__(f"bexio GET {path} failed with HTTP {status}")
        self.status = status
        self.path = path
        self.detail = detail


TOKEN_NAMES = {
    "oauth": "OAuth refresh token",
    "pat": "read-scoped personal access token (BEXIO_READ_TOKEN)",
}


def resolve_token():
    """(token, kind) for the API: the read token first, the OAuth login only without one.

    kind is "oauth" or "pat". Reads take BEXIO_READ_TOKEN when it is set: the
    OAuth login is not touched at all, so a read neither spends nor rotates
    the stored refresh token. Without a read token the OAuth login runs, and
    a refresh token that exists but is refused is an error, not a reason to
    fall back. The export asks for no token of its own: it reads with the one
    token chosen here, as every script does.
    """
    read_token = os.environ.get("BEXIO_READ_TOKEN")
    if read_token:
        return read_token, "pat"
    import launcher  # lazy: only the OAuth path needs it

    oauth = launcher.load(
        "oauth.py"
    )  # oauth.py is a bash launcher; its python body is what runs here

    if oauth.read_refresh_token(oauth.REFRESH_KEY) is not None:
        try:
            return oauth.refresh_access_token(), "oauth"
        except (oauth.OAuthError, oauth.VaultError) as err:
            raise SystemExit(f"bexio login: {err}")
    raise SystemExit(
        "no bexio read token (BEXIO_READ_TOKEN is not set) and no refresh token ({} is not set); "
        "log in with `{}` (see finance/bexio/README.md)".format(
            oauth.REFRESH_KEY, oauth.LOGIN_COMMAND
        )
    )


class Client:
    def __init__(self, token=None, base_url=BASE_URL):
        if token is None:
            token, self.token_kind = resolve_token()
            print(f"bexio token: {TOKEN_NAMES[self.token_kind]}", file=sys.stderr)
        else:
            self.token_kind = "given"
        self._token = token
        self._base_url = base_url.rstrip("/")
        self.requests = 0

    def _open(self, req):
        return urllib.request.urlopen(req, timeout=60)

    def get(self, path, params=None, raw=False, accept=None):
        """GET one resource; returns the decoded JSON body, or the bytes when raw (a file's content).

        accept sets the Accept header: a raw request sends */* and the others application/json, unless told otherwise.
        """
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(MAX_RETRIES + 1):
            self.requests += 1
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", accept or ("*/*" if raw else "application/json"))
            req.add_header("Authorization", "Bearer " + self._token)
            req.add_header("User-Agent", "yardr-finance-bexio-inventory/1.0")
            try:
                with self._open(req) as resp:
                    body = resp.read()
                return body if raw else json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as err:
                retryable = err.code == 429 or 500 <= err.code < 600
                if not retryable or attempt == MAX_RETRIES:
                    raise BexioError(err.code, path, _error_detail(err)) from None
                time.sleep(_wait_seconds(err, attempt))
            except urllib.error.URLError:
                if attempt == MAX_RETRIES:
                    raise BexioError("network", path) from None
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def paginate(self, path, params=None, page_size=PAGE_SIZE):
        """Yield every record of a list endpoint, page by page (limit and offset).

        bexio may cap a page below the size asked for, so the offset advances
        by the rows actually received and the walk ends on an empty page. A
        short page is not taken as the last one. Some endpoints ignore limit
        and offset and answer every page with the same rows (/3.0/taxes and
        /3.0/accounting/business_years do); the walk therefore ends too when a
        page brings no id not seen before, and a row is yielded once per id.
        """
        seen = set()
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params["limit"] = page_size
            page_params["offset"] = offset
            page = self.get(path, page_params)
            if not isinstance(page, list):
                raise BexioError("unexpected body", path)
            if not page:
                return
            fresh = _unseen(page, seen)
            if not fresh:
                return
            yield from fresh
            offset += len(page)

    def paginate_pages(
        self, path, size_param, rows_key=None, params=None, page_size=PAGE_SIZE
    ):
        """Yield every record of a page-numbered endpoint (the API 4.0 lists).

        These take `page` and a size parameter whose name differs by endpoint
        (`page_size` for purchase bills, `per-page` for banking payments), and
        wrap the records in an object: under `rows_key` when given. The walk
        ends on an empty page or one without a new id, as in paginate().
        """
        seen = set()
        page = 1
        while True:
            page_params = dict(params or {})
            page_params["page"] = page
            page_params[size_param] = page_size
            body = self.get(path, page_params)
            rows = body.get(rows_key) if rows_key else body
            if not isinstance(rows, list):
                raise BexioError("unexpected body", path)
            if not rows:
                return
            fresh = _unseen(rows, seen)
            if not fresh:
                return
            yield from fresh
            page += 1


def _unseen(rows, seen):
    """The rows whose id is new, remembering them in `seen`.

    A row without an id (not a dict, or no "id" key) always counts as new, so
    lists of plain values or id-less records page as before.
    """
    fresh = []
    for row in rows:
        key = row.get("id") if isinstance(row, dict) else None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        fresh.append(row)
    return fresh


def _error_detail(err, limit=500):
    """The first bytes of a refused answer's body, as text; None when there is no body to read."""
    try:
        return err.read(limit).decode("utf-8", errors="replace") or None
    except (OSError, AttributeError):
        return None


def _wait_seconds(err, attempt):
    # bexio sends Retry-After on 429; fall back to exponential backoff.
    retry_after = err.headers.get("Retry-After") if err.headers else None
    try:
        return max(1.0, float(retry_after))
    except (TypeError, ValueError):
        return float(2**attempt)

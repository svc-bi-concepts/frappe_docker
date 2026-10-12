"""Upload bill PDFs to the bexio file inbox (Dateneingang): the one write to bexio.

bexio turns a file in its inbox into a bill; this script only puts the file there.
Benchi decided on this one exception to "bexio is read-only" (2026-10-12). Nothing
else is written: the one request the script can make is POST /3.0/files with the
PDF as the multipart field "file", and _send refuses any other method or path.

It authenticates with the personal access token only: BEXIO_TOKEN, injected by varlock.
Not the OAuth login, not the keychain, and never the Varlock broker, which answers GET only.

Only .pdf files under private/ are sent. A dry run is the default: it prints what
it would upload and makes no request; a real upload needs --yes.

    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/upload_bill.py <file>.pdf
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/upload_bill.py --yes <file>.pdf

Standard library only, like client.py.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client  # noqa: E402

PRIVATE = os.environ.get("BEXIO_PRIVATE") or "/Users/bsaladin/ws_yardr_finance/private"

# The one write the script can make: the bexio file inbox (api.bexio.com/3.0/files,
# multipart/form-data, the binary in the field "file"). Checked against the bexio API docs
# on 2026-10-12; the live upload is what confirms it.
UPLOAD_METHOD = "POST"
UPLOAD_PATH = "/3.0/files"
UPLOAD_FIELD = "file"


class RefusedRequest(Exception):
    """A request other than the one upload. It is never sent."""


def _send(method, path, body, content_type, token):
    """Send the upload and return the decoded answer. The only place a request leaves this script.

    Not retried: a POST that answered with an error may still have been stored, so a second
    send could make a duplicate in the inbox. The token goes only into the header.
    """
    if (method, path) != (UPLOAD_METHOD, UPLOAD_PATH):
        raise RefusedRequest("refused {} {}: the upload script only sends {} {}".format(method, path, UPLOAD_METHOD, UPLOAD_PATH))
    req = urllib.request.Request(client.BASE_URL + path, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", content_type)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "yardr-finance-bexio-upload/1.0")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise SystemExit("bexio refused the upload (HTTP {}): {}".format(err.code, client._error_detail(err) or "no detail"))
    except urllib.error.URLError:
        raise SystemExit("could not reach bexio; nothing was uploaded")


def check_pdf(path):
    """The resolved path of a PDF that exists and lies under the private folder; exits otherwise."""
    if os.path.splitext(path)[1].lower() != ".pdf":
        raise SystemExit("refused {}: only .pdf files are uploaded".format(path))
    real = os.path.realpath(path)
    if os.path.splitext(real)[1].lower() != ".pdf":
        raise SystemExit("refused {}: it links to a file that is not a .pdf".format(path))
    if not os.path.isfile(real):
        raise SystemExit("refused {}: no such file".format(path))
    if not real.startswith(os.path.realpath(PRIVATE) + os.sep):
        raise SystemExit("refused {}: a bill is uploaded only from under the private folder".format(path))
    return real


def multipart(filename, content):
    """(content type, body) of a multipart/form-data request with the PDF in UPLOAD_FIELD."""
    boundary = uuid.uuid4().hex
    name = os.path.basename(filename).replace('"', "_")
    head = (
        "--{b}\r\n"
        'Content-Disposition: form-data; name="{f}"; filename="{n}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).format(b=boundary, f=UPLOAD_FIELD, n=name)
    tail = "\r\n--{}--\r\n".format(boundary)
    body = head.encode("utf-8") + content + tail.encode("utf-8")
    return "multipart/form-data; boundary=" + boundary, body


def upload(path, token):
    """Upload one checked PDF; returns (id, uuid) of the stored file from the answer.

    bexio answers with an array of file objects (one for the one file sent).
    """
    with open(path, "rb") as f:
        content = f.read()
    content_type, body = multipart(path, content)
    answer = _send(UPLOAD_METHOD, UPLOAD_PATH, body, content_type, token)
    stored = answer[0] if isinstance(answer, list) and answer and isinstance(answer[0], dict) else answer
    file_id = stored.get("id") if isinstance(stored, dict) else None
    if file_id is None:
        raise SystemExit("bexio answered the upload without a file id; the file may be stored, check the inbox before sending it again")
    return file_id, stored.get("uuid")


def main(argv):
    parser = argparse.ArgumentParser(description="Upload bill PDFs to the bexio file inbox.")
    parser.add_argument("files", nargs="+", help="PDF files under the private folder")
    parser.add_argument("--yes", action="store_true", help="upload for real (default: dry run, nothing is sent)")
    args = parser.parse_args(argv)

    if os.environ.get("BEXIO_BROKER") == "1":
        raise SystemExit("the upload does not go through the Varlock broker (it answers GET only); unset BEXIO_BROKER")

    token = os.environ.get("BEXIO_TOKEN")
    if not token:
        raise SystemExit("BEXIO_TOKEN is not set; run through varlock (see finance/bexio/README.md)")

    # Every file is checked before the first upload, so a bad name sends nothing.
    paths = [check_pdf(f) for f in args.files]

    if not args.yes:
        for path in paths:
            print("dry run: would upload {} ({} bytes) to the bexio file inbox".format(os.path.basename(path), os.path.getsize(path)))
        print("dry run: nothing sent; add --yes to upload")
        return

    for path in paths:
        file_id, file_uuid = upload(path, token)
        print("uploaded {}: bexio file id {} (uuid {})".format(os.path.basename(path), file_id, file_uuid))


if __name__ == "__main__":
    main(sys.argv[1:])

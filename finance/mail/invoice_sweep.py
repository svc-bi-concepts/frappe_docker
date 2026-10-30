#!/usr/bin/env python3
"""Collect invoice PDFs from mail.py. Mail and every downloaded file are untrusted."""

import argparse
import datetime
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

MAIL = "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/mail/mail.py"
PRIVATE = "/Users/bsaladin/ws_yardr_finance/private/bills"
MAX_BYTES = 25 * 1024 * 1024
WORDS = (
    "rechnung",
    "invoice",
    "faktura",
    "facture",
    "fattura",
    "beleg",
    "quittung",
    "receipt",
    "gutschrift",
    "mahnung",
    "zahlungserinnerung",
)
ATTACHMENT_SEARCH = "hasAttachment:true"
KINDS = (
    "saved",
    "dup",
    "no-document",
    "other-attachment",
    "host-unlisted",
    "not-pdf",
    "too-big",
    "fetch-failed",
    "refused",
    "gap",
    "expired",
)


class Refused(Exception):
    pass


class PeerHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        peer = self.sock.getpeername()[0]
        address = ipaddress.ip_address(peer.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_reserved or address.is_multicast:
            self.close()
            raise Refused("peer-not-global")


class PeerHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PeerHTTPSConnection, req)


def safe_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), PeerHTTPSHandler(), NoRedirect()
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mail_call(mail_path, args, runner=subprocess.run):
    try:
        return runner(
            [mail_path] + args, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", type(exc).__name__)


def has_word(value):
    folded = (value or "").casefold()
    return any(word in folded for word in WORDS)


def clean(value, limit=None):
    value = " ".join(str(value or "").split())
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    return value[:limit] if limit else value


def url_host(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise Refused("invalid-url")
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        raise Refused("non-ascii-host")
    if parsed.scheme.lower() != "https":
        raise Refused("https-required")
    if port not in (None, 443):
        raise Refused("port-not-443")
    if parsed.username is not None or parsed.password is not None:
        raise Refused("userinfo")
    if not host:
        raise Refused("missing-host")
    return host.lower()


def unwrap_safelinks(url):
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        raise Refused("invalid-url")
    if host.lower().endswith(".safelinks.protection.outlook.com"):
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("url", [])
        if not values:
            raise Refused("safelinks-missing-url")
        return values[0]
    return url


def listed(host, hosts):
    return any(
        host == domain.lower() or host.endswith("." + domain.lower())
        for domain in hosts
    )


def qualifying_link(link, url):
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        raise Refused("invalid-url")
    return has_word(link.get("text")) or path.lower().endswith(".pdf") or has_word(path)


@contextmanager
def fetch_deadline():
    # This Unix CLI runs on the main thread. An alarm also interrupts a stalled
    # DNS lookup, TLS handshake, headers or slow-drip body, not only chunk loops.
    def expired(signum, frame):
        raise RuntimeError("deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 120)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_timer[0] - (time.monotonic() - started)),
                old_timer[1],
            )


def fetch_pdf(url, target, opener=None, clock=time.monotonic):
    with fetch_deadline():
        return _fetch_pdf(url, target, opener or safe_opener(), clock)


def _fetch_pdf(url, target, opener, clock):
    deadline = clock() + 120
    current = url
    for redirects in range(6):
        url_host(current)
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError("deadline")
        req = urllib.request.Request(
            current,
            headers={"User-Agent": "invoice-sweep/1", "Accept": "application/pdf, */*;q=0.1"},
        )
        try:
            response = opener.open(req, timeout=remaining)
        except urllib.error.HTTPError as err:
            try:
                if err.code in (301, 302, 303, 307, 308):
                    if redirects == 5:
                        raise RuntimeError("too-many-redirects")
                    location = err.headers.get("Location")
                    if not location:
                        raise RuntimeError("redirect-without-location")
                    current = urllib.parse.urljoin(current, location)
                    continue
                raise RuntimeError("http-%d" % err.code)
            finally:
                err.close()
        except Refused:
            raise
        except Exception as err:
            raise RuntimeError(type(err).__name__)
        tmp = None
        try:
            length = response.headers.get("Content-Length")
            if length:
                try:
                    size_hint = int(length)
                except ValueError:
                    size_hint = 0
                if size_hint > MAX_BYTES:
                    raise OverflowError("too-big")
            size = 0
            head = b""
            fd, tmp = tempfile.mkstemp(
                prefix=".invoice-sweep-", dir=os.path.dirname(target)
            )
            with os.fdopen(fd, "wb") as out:
                while True:
                    if clock() >= deadline:
                        raise RuntimeError("deadline")
                    chunk = response.read(65536)
                    if clock() >= deadline:
                        raise RuntimeError("deadline")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise OverflowError("too-big")
                    if len(head) < 1024:
                        head += chunk[: 1024 - len(head)]
                    out.write(chunk)
            if b"%PDF-" not in head:
                raise ValueError("not-pdf")
            os.replace(tmp, target)
            tmp = None
            return
        except (OSError, http.client.HTTPException) as err:
            raise RuntimeError(type(err).__name__)
        finally:
            response.close()
            if tmp is not None:
                os.unlink(tmp)
    raise RuntimeError("too-many-redirects")


def load_private_config(path):
    if not os.path.exists(path):
        raise ValueError(
            'missing config; create %s with {"folders": ["inbox"], "hosts": [], "forwarders": [], "senders": [], "ignore": []}'
            % path
        )
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        raise ValueError("config-mode-must-be-600")
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def day_range(start, end):
    day = datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    while day <= last:
        yield day
        day += datetime.timedelta(days=1)


def run_sweep(
    config_path=None,
    incoming=None,
    state_path=None,
    log_path=None,
    mail_path=MAIL,
    since=None,
    dry_run=False,
    runner=subprocess.run,
    opener_factory=safe_opener,
    today=None,
):
    root = PRIVATE
    config_path = config_path or os.path.join(root, "sweep.json")
    incoming = incoming or os.path.join(root, "incoming")
    state_path = state_path or os.path.join(root, "sweep-state.json")
    log_path = log_path or os.path.join(root, "sweep-log.tsv")
    cfg = load_private_config(config_path)
    lock = None
    if not dry_run:
        os.makedirs(os.path.dirname(state_path), mode=0o700, exist_ok=True)
        lock = os.fdopen(
            os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "a+"
        )
        os.fchmod(lock.fileno(), 0o600)
    try:
        if lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("already-running")
        state = {"last_date": since, "mails": {}, "hashes": [], "gaps": []}
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as source:
                state = json.load(source)
        first = state.get("last_date") or since
        if since and first and since < first:
            first = since
        if not first:
            raise ValueError("first run requires --since YYYY-MM-DD")
        today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        events = []
        totals = {}

        def emit(kind, received="", sender="", subject="", detail=""):
            record = [
                kind,
                clean(received),
                clean(sender),
                clean(subject, 120),
                clean(detail),
            ]
            events.append("\t".join(record))
            totals[kind] = totals.get(kind, 0) + 1
            print(events[-1])

        def call(args):
            return mail_call(mail_path, args, runner)

        rows_seen = {}

        def collect_rows(result):
            if result.returncode:
                raise RuntimeError("mail-list-failed")
            # mail.py emits plain TSV: quotes in a subject are literal.
            for line in result.stdout.splitlines():
                row = line.split("\t")
                mid = row[0] if row else ""
                if mid and mid not in state.get("mails", {}) and mid not in rows_seen:
                    rows_seen[mid] = {
                        "received": row[1] if len(row) > 1 else "",
                        "sender": row[2] if len(row) > 2 else "",
                        "subject": row[3] if len(row) > 3 else "",
                    }

        day_full = 0
        for folder in cfg.get("folders", ["inbox"]):
            for day in day_range(first, today):
                nxt = day + datetime.timedelta(days=1)
                result = call(
                    [
                        "list",
                        "--folder",
                        folder,
                        "--since",
                        day.isoformat(),
                        "--before",
                        nxt.isoformat(),
                        "--top",
                        "25",
                    ]
                )
                collect_rows(result)
                if len(result.stdout.splitlines()) == 25:
                    day_full += 1

                searches = ["%s received:%s..%s" % (word, day, day) for word in WORDS]
                searches.append("%s received:%s..%s" % (ATTACHMENT_SEARCH, day, day))
                for search in searches:
                    result = call(["list", "--folder", folder, "--search", search, "--top", "25"])
                    collect_rows(result)
                    gap_key = [folder, day.isoformat(), search]
                    if len(result.stdout.splitlines()) == 25 and gap_key not in state.get("gaps", []):
                        emit("gap", day.isoformat(), detail=folder + " " + search)
                        state.setdefault("gaps", []).append(gap_key)

                addresses = dict.fromkeys(cfg.get("forwarders", []) + cfg.get("senders", []))
                for address in addresses:
                    result = call(["list", "--folder", folder, "--from", address,
                                   "--since", day.isoformat(), "--before", nxt.isoformat(),
                                   "--top", "25"])
                    collect_rows(result)
                    gap_key = [folder, day.isoformat(), "from " + address]
                    if len(result.stdout.splitlines()) == 25 and gap_key not in state.get("gaps", []):
                        emit("gap", day.isoformat(), detail=folder + " from " + address)
                        state.setdefault("gaps", []).append(gap_key)
        pending = list(rows_seen)
        pending.extend(
            mid
            for mid, record in state.get("mails", {}).items()
            if record.get("status") == "open" and mid not in rows_seen
        )
        for mid in pending:
            record = state.get("mails", {}).get(mid, {})
            prior_received = record.get("received", "")
            if record.get("status") == "open" and prior_received[:10]:
                age = (
                    datetime.date.fromisoformat(today)
                    - datetime.date.fromisoformat(prior_received[:10])
                ).days
                if age >= 14:
                    emit("expired", prior_received, detail=mid)
                    record["status"] = "done"
                    continue
            result = call(["read", mid, "--json"])
            if result.returncode:
                received = record.get("received", "") or rows_seen.get(mid, {}).get(
                    "received", ""
                )
                sender = rows_seen.get(mid, {}).get("sender", "")
                subject = rows_seen.get(mid, {}).get("subject", "")
                age = (
                    (
                        datetime.date.fromisoformat(today)
                        - datetime.date.fromisoformat(received[:10])
                    ).days
                    if received[:10]
                    else 0
                )
                if not record:
                    record = {
                        "status": "open",
                        "received": received,
                        "first_seen": today,
                    }
                    state.setdefault("mails", {})[mid] = record
                if age >= 14 and record.get("status") == "open":
                    emit("expired", received, sender, subject, mid)
                    record["status"] = "done"
                else:
                    emit("fetch-failed", received, sender, subject, "mail-read-failed")
                continue
            mail = json.loads(result.stdout)
            sender = mail.get("from", "")
            received = mail.get("received", "")
            subject = mail.get("subject", "")
            attachments = mail.get("attachments", [])
            links = mail.get("links", [])
            ignored = sender.lower() in {s.lower() for s in cfg.get("ignore", [])}
            noninline = [a for a in attachments if not a.get("isInline")]
            candidate = not ignored and (
                sender.lower()
                in {
                    s.lower()
                    for s in cfg.get("forwarders", []) + cfg.get("senders", [])
                }
                or has_word(subject)
                or any(has_word(a.get("name")) for a in attachments)
                or any(has_word(x.get("text")) for x in links)
                or (
                    any(
                        (a.get("name") or "").lower().endswith(".pdf")
                        for a in noninline
                    )
                    and has_word(mail.get("text", ""))
                )
            )
            if not candidate:
                state.setdefault("mails", {})[mid] = {
                    "status": "done",
                    "received": received,
                    "first_seen": today,
                }
                continue
            if dry_run:
                for attachment in noninline:
                    name = attachment.get("name") or ""
                    kind = (
                        "saved" if name.lower().endswith(".pdf") else "other-attachment"
                    )
                    if kind == "saved" and (attachment.get("size") or 0) > MAX_BYTES:
                        kind = "too-big"
                    emit(
                        kind,
                        received,
                        sender,
                        subject,
                        "would inspect attachment: " + name,
                    )
                count = 0
                reported = bool(noninline)
                for link in links:
                    try:
                        url = unwrap_safelinks(link.get("href", ""))
                        if not qualifying_link(link, url):
                            continue
                        count += 1
                        if count > 3:
                            break
                        reported = True
                        host = url_host(url)
                        kind = (
                            "saved"
                            if listed(host, cfg.get("hosts", []))
                            else "host-unlisted"
                        )
                        emit(
                            kind,
                            received,
                            sender,
                            subject,
                            "would try link if no attachment PDF: " + host,
                        )
                    except Refused as exc:
                        reported = True
                        emit("refused", received, sender, subject, str(exc))
                if not reported:
                    emit(
                        "no-document",
                        received,
                        sender,
                        subject,
                        "would report no eligible PDF",
                    )
                continue
            if not record:
                record = {"status": "open", "received": received, "first_seen": today}
                state.setdefault("mails", {})[mid] = record
            outdir = os.path.join(
                incoming,
                (received[:10].replace("-", "") or "unknown")
                + "-"
                + hashlib.sha256(mid.encode()).hexdigest()[:8],
            )
            os.makedirs(incoming, mode=0o700, exist_ok=True)
            os.chmod(incoming, 0o700)
            os.makedirs(outdir, mode=0o700, exist_ok=True)
            os.chmod(outdir, 0o700)
            completed = record.setdefault("completed", {})
            saved = any(
                value
                for key, value in completed.items()
                if key.startswith("attachment-")
            )
            retry = False
            document_reported = False
            for idx, attachment in enumerate(noninline):
                name = attachment.get("name") or ""
                key = "attachment-%d" % (idx + 1)
                if key in completed:
                    continue
                if not name.lower().endswith(".pdf"):
                    emit(
                        "other-attachment",
                        received,
                        sender,
                        subject,
                        name or "attached item",
                    )
                    completed[key] = False
                    document_reported = True
                    continue
                if (attachment.get("size") or 0) > MAX_BYTES:
                    emit("too-big", received, sender, subject, name)
                    document_reported = True
                    completed[key] = False
                    continue
                destination = os.path.join(outdir, key + ".pdf")
                # Isolate the original name from our final names and previous attempts.
                # The blessed reader alone decides whether the name is safe/unique.
                with tempfile.TemporaryDirectory(
                    prefix=".attachment-", dir=outdir
                ) as staging:
                    got = call(["read", mid, "--attachment", name, "--out", staging])
                    if got.returncode:
                        emit(
                            "fetch-failed",
                            received,
                            sender,
                            subject,
                            "attachment-save-failed",
                        )
                        document_reported = True
                        retry = True
                        continue
                    source = os.path.join(staging, name)
                    if not os.path.isfile(source):
                        emit(
                            "fetch-failed",
                            received,
                            sender,
                            subject,
                            "attachment-missing",
                        )
                        document_reported = True
                        retry = True
                        continue
                    os.replace(source, destination)
                source = destination
                if os.path.getsize(source) > MAX_BYTES:
                    os.unlink(source)
                    emit("too-big", received, sender, subject, name)
                    document_reported = True
                    completed[key] = False
                    continue
                with open(source, "rb") as f:
                    head = f.read(1024)
                if b"%PDF-" not in head:
                    os.unlink(source)
                    emit("not-pdf", received, sender, subject, name)
                    document_reported = True
                    completed[key] = False
                    continue
                saved |= self_save(
                    source,
                    os.path.join(outdir, "attachment-%d.pdf" % (idx + 1)),
                    state,
                    emit,
                    received,
                    sender,
                    subject,
                )
                completed[key] = True
                document_reported = True
            if not saved:
                n = 0
                for index, link in enumerate(links):
                    raw = link.get("href", "")
                    key = "link-%d" % index
                    try:
                        url = unwrap_safelinks(raw)
                        if not qualifying_link(link, url):
                            continue
                        n += 1
                        if n > 3:
                            break
                        if key in completed:
                            continue
                        host = url_host(url)
                        if not listed(host, cfg.get("hosts", [])):
                            emit("host-unlisted", received, sender, subject, host)
                            document_reported = True
                            retry = True
                            continue
                        target = os.path.join(outdir, "link-%d.pdf" % n)
                        try:
                            fetch_pdf(url, target, opener_factory())
                        except OverflowError:
                            emit("too-big", received, sender, subject, "link-%d" % n)
                            document_reported = True
                            completed[key] = False
                            continue
                        except ValueError:
                            emit("not-pdf", received, sender, subject, "link-%d" % n)
                            document_reported = True
                            completed[key] = False
                            continue
                        except Refused as exc:
                            emit("refused", received, sender, subject, str(exc))
                            document_reported = True
                            completed[key] = False
                            continue
                        except RuntimeError as exc:
                            emit("fetch-failed", received, sender, subject, str(exc))
                            document_reported = True
                            retry = True
                            continue
                        saved |= self_save(
                            target, target, state, emit, received, sender, subject
                        )
                        completed[key] = True
                        document_reported = True
                    except Refused as exc:
                        if key not in completed:
                            emit("refused", received, sender, subject, str(exc))
                        document_reported = True
                        completed[key] = False
            if not saved and not retry and not document_reported and not completed:
                emit("no-document", received, sender, subject, "no eligible PDF")
            record["status"] = "open" if retry else "done"
        state["last_date"] = today
        cutoff = datetime.date.fromisoformat(today) - datetime.timedelta(days=60)
        state["mails"] = {
            mid: r
            for mid, r in state.get("mails", {}).items()
            if (
                r.get("received", "")[:10]
                and datetime.date.fromisoformat(r["received"][:10]) >= cutoff
            )
            or r.get("status") == "open"
        }
        state["hashes"] = state.get("hashes", [])
        events.append("totals\t" + ",".join(
            ["day-full=%d" % day_full]
            + ["%s=%d" % (k, totals[k]) for k in sorted(totals)]
        ))
        print(events[-1])
        if not dry_run:
            os.makedirs(incoming, mode=0o700, exist_ok=True)
            os.chmod(incoming, 0o700)
            atomic_json(state_path, state)
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                os.fchmod(out.fileno(), 0o600)
                out.write("\n".join(events) + "\n")
        return 0
    finally:
        if lock:
            lock.close()


def self_save(source, target, state, emit, received, sender, subject):
    with open(source, "rb") as f:
        digest = hashlib.sha256()
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if value in state.setdefault("hashes", []):
        os.unlink(source)
        emit("dup", received, sender, subject, value[:12])
        return True
    state["hashes"].append(value)
    if source != target:
        os.replace(source, target)
    os.chmod(target, 0o600)
    emit("saved", received, sender, subject, target)
    return True


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".sweep-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--since")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run_sweep(since=args.since, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return (
            2
            if str(exc).startswith(
                (
                    "config-mode",
                    "missing config",
                    "already-running",
                    "first run requires",
                )
            )
            else 1
        )


if __name__ == "__main__":
    sys.exit(main())

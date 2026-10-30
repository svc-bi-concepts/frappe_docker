#!/usr/bin/env python3
"""Collect invoice PDFs from mail.py. Mail and every downloaded file are untrusted."""

import argparse
import datetime
import csv
import io
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAIL = "/Users/bsaladin/ws_yardr_finance/frappe_docker/finance/mail/mail.py"
PRIVATE = "/Users/bsaladin/ws_yardr_finance/private/bills"
MAX_BYTES = 25 * 1024 * 1024
WORDS = ("rechnung", "invoice", "faktura", "facture", "fattura", "beleg", "quittung", "receipt", "gutschrift", "mahnung", "zahlungserinnerung")
KINDS = ("saved", "dup", "no-document", "other-attachment", "host-unlisted", "not-pdf", "too-big", "fetch-failed", "refused", "gap", "expired")


class Refused(Exception):
    pass


class PeerHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        peer = self.sock.getpeername()[0]
        address = ipaddress.ip_address(peer.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            self.close()
            raise Refused("peer-not-global")


class PeerHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PeerHTTPSConnection, req)


def safe_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), PeerHTTPSHandler(), NoRedirect())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mail_call(mail_path, args, runner=subprocess.run):
    try:
        return runner([mail_path] + args, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", type(exc).__name__)


def parse_json_result(result):
    if result.returncode:
        raise RuntimeError("mail-read-failed")
    return json.loads(result.stdout)


def has_word(value):
    folded = (value or "").casefold()
    return any(word in folded for word in WORDS)


def clean(value, limit=None):
    value = " ".join(str(value or "").split())
    return value[:limit] if limit else value


def url_host(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        raise Refused("non-ascii-host")
    if parsed.scheme.lower() != "https":
        raise Refused("https-required")
    if parsed.port not in (None, 443):
        raise Refused("port-not-443")
    if parsed.username is not None or parsed.password is not None:
        raise Refused("userinfo")
    if not host:
        raise Refused("missing-host")
    return host.lower()


def unwrap_safelinks(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host.lower().endswith(".safelinks.protection.outlook.com"):
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("url", [])
        if not values:
            raise Refused("safelinks-missing-url")
        return values[0]
    return url


def listed(host, hosts):
    return any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in hosts)


def fetch_pdf(url, target, opener=None, clock=time.monotonic):
    opener = opener or safe_opener()
    deadline = clock() + 120
    current = url
    for redirects in range(6):
        url_host(current)
        if clock() >= deadline:
            raise RuntimeError("deadline")
        req = urllib.request.Request(current, headers={"User-Agent": "invoice-sweep/1", "Accept": "application/pdf"})
        try:
            response = opener.open(req, timeout=max(1, deadline-clock()))
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308):
                if redirects == 5:
                    raise RuntimeError("too-many-redirects")
                location = err.headers.get("Location")
                if not location:
                    raise RuntimeError("redirect-without-location")
                current = urllib.parse.urljoin(current, location)
                continue
            raise RuntimeError("http-%d" % err.code)
        except Refused:
            raise
        except Exception as err:
            raise RuntimeError(type(err).__name__)
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_BYTES:
                    raise OverflowError("too-big")
            except ValueError:
                pass
        size = 0
        head = b""
        fd, tmp = tempfile.mkstemp(prefix=".invoice-sweep-", dir=os.path.dirname(target))
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    if clock() >= deadline:
                        raise RuntimeError("deadline")
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise OverflowError("too-big")
                    if len(head) < 1024:
                        head += chunk[:1024-len(head)]
                    out.write(chunk)
            if b"%PDF-" not in head:
                raise ValueError("not-pdf")
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
            return
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    raise RuntimeError("too-many-redirects")


def load_private_config(path):
    if not os.path.exists(path):
        raise ValueError('missing config; create %s with {"folders": ["inbox"], "hosts": [], "forwarders": [], "senders": [], "ignore": []}' % path)
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


def row_id(row):
    return row.get("id") or row.get("Id") or ""


def run_sweep(config_path=None, incoming=None, state_path=None, log_path=None, mail_path=MAIL,
              since=None, dry_run=False, runner=subprocess.run, opener_factory=safe_opener, today=None):
    root = PRIVATE
    config_path = config_path or os.path.join(root, "sweep.json")
    incoming = incoming or os.path.join(root, "incoming")
    state_path = state_path or os.path.join(root, "sweep-state.json")
    log_path = log_path or os.path.join(root, "sweep-log.tsv")
    cfg = load_private_config(config_path)
    lock = None
    if not dry_run:
        os.makedirs(os.path.dirname(state_path), mode=0o700, exist_ok=True)
        lock = open(state_path + ".lock", "a+")
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
        if not first:
            raise ValueError("first run requires --since YYYY-MM-DD")
        today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        events = []
        totals = {}
        def emit(kind, received="", sender="", subject="", detail=""):
            record = [kind, clean(received), clean(sender), clean(subject, 120), clean(detail)]
            events.append("\t".join(record))
            totals[kind] = totals.get(kind, 0) + 1
            print(events[-1])
        def call(args):
            return mail_call(mail_path, args, runner)
        rows_seen = {}
        try:
            for folder in cfg.get("folders", ["inbox"]):
                for day in day_range(first, today):
                    nxt = day + datetime.timedelta(days=1)
                    result = call(["list", "--folder", folder, "--since", day.isoformat(), "--before", nxt.isoformat(), "--top", "25"])
                    if result.returncode:
                        raise RuntimeError("mail-list-failed")
                    rows = list(csv.reader(io.StringIO(result.stdout), delimiter="\t"))
                    if len(rows) == 25 and [folder, day.isoformat()] not in state.get("gaps", []):
                        emit("gap", day.isoformat(), detail=folder)
                        state.setdefault("gaps", []).append([folder, day.isoformat()])
                    for row in rows:
                        mid = row[0] if row else ""
                        if mid and mid not in state.get("mails", {}):
                            rows_seen[mid] = {"received": row[1] if len(row) > 1 else "", "sender": row[2] if len(row) > 2 else "", "subject": row[3] if len(row) > 3 else ""}
        except Exception:
            if not dry_run:
                raise
            raise
        pending = list(rows_seen)
        pending.extend(mid for mid, record in state.get("mails", {}).items() if record.get("status") == "open" and mid not in rows_seen)
        for mid in pending:
            record = state.get("mails", {}).get(mid, {})
            prior_received = record.get("received", "")
            if record.get("status") == "open" and prior_received[:10]:
                age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(prior_received[:10])).days
                if age >= 14:
                    emit("expired", prior_received, detail=mid)
                    record["status"] = "done"
                    continue
            result = call(["read", mid, "--json"])
            if result.returncode:
                received = record.get("received", "") or rows_seen.get(mid, {}).get("received", "")
                sender = rows_seen.get(mid, {}).get("sender", "")
                subject = rows_seen.get(mid, {}).get("subject", "")
                first_seen = record.get("first_seen", today)
                age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(received[:10])).days if received[:10] else 0
                if not record:
                    record = {"status":"open", "received":received, "first_seen":today}
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
            candidate = not ignored and (sender.lower() in {s.lower() for s in cfg.get("forwarders", []) + cfg.get("senders", [])} or has_word(subject) or any(has_word(a.get("name")) for a in noninline) or any(has_word(x.get("text")) for x in links) or (any((a.get("name") or "").lower().endswith(".pdf") for a in noninline) and has_word(mail.get("text", ""))))
            if not candidate:
                state.setdefault("mails", {}).pop(mid, None)
                continue
            if dry_run:
                emit("saved", received, sender, subject, "would collect documents")
                continue
            if not record:
                record = {"status": "open", "received": received, "first_seen": today}
                state.setdefault("mails", {})[mid] = record
            outdir = os.path.join(incoming, (received[:10].replace("-", "") or "unknown") + "-" + hashlib.sha256(mid.encode()).hexdigest()[:8])
            os.makedirs(outdir, mode=0o700, exist_ok=True); os.chmod(outdir, 0o700)
            saved = False; retry = False; document_reported = False
            for idx, attachment in enumerate(noninline):
                name = attachment.get("name") or ""
                if not name.lower().endswith(".pdf"): continue
                if (attachment.get("size") or 0) > MAX_BYTES:
                    emit("too-big", received, sender, subject, name); document_reported = True; record["status"]="done"; continue
                destination = os.path.join(outdir, "attachment-%d.pdf" % (idx+1))
                got = call(["read", mid, "--attachment", name, "--out", outdir])
                if got.returncode:
                    emit("fetch-failed", received, sender, subject, "attachment-save-failed"); document_reported = True; retry=True; continue
                # mail.py writes the original safe attachment name into outdir.
                source = os.path.join(outdir, name)
                if not os.path.exists(source):
                    emit("fetch-failed", received, sender, subject, "attachment-missing"); document_reported = True; retry=True; continue
                if os.path.getsize(source) > MAX_BYTES:
                    os.unlink(source); emit("too-big", received, sender, subject, name); document_reported = True; record["status"]="done"; continue
                with open(source, "rb") as f: head=f.read(1024)
                if b"%PDF-" not in head:
                    os.unlink(source); emit("not-pdf", received, sender, subject, name); document_reported = True; record["status"]="done"; continue
                saved |= self_save(source, os.path.join(outdir, "attachment-%d.pdf" % (idx+1)), state, emit, received, sender, subject)
            if not saved:
                for n, link in enumerate(links[:3], 1):
                    raw = link.get("href", "")
                    try:
                        url = unwrap_safelinks(raw); host = url_host(url)
                        if not (has_word(link.get("text")) or urllib.parse.urlsplit(url).path.lower().endswith(".pdf") or has_word(urllib.parse.urlsplit(url).path)):
                            continue
                        if not listed(host, cfg.get("hosts", [])):
                            emit("host-unlisted", received, sender, subject, host); document_reported = True; retry=True; continue
                        target = os.path.join(outdir, "link-%d.pdf" % n)
                        try: fetch_pdf(url, target, opener_factory())
                        except OverflowError: emit("too-big", received, sender, subject, "link-%d" % n); document_reported = True; record["status"]="done"; continue
                        except ValueError: emit("not-pdf", received, sender, subject, "link-%d" % n); document_reported = True; record["status"]="done"; continue
                        except Refused as exc: emit("refused", received, sender, subject, str(exc)); document_reported = True; retry=True; continue
                        except RuntimeError as exc: emit("fetch-failed", received, sender, subject, str(exc)); document_reported = True; retry=True; continue
                        saved |= self_save(target, target, state, emit, received, sender, subject)
                    except Refused as exc:
                        emit("refused", received, sender, subject, str(exc)); document_reported = True; retry=True
            if not saved and not retry and not document_reported and not any((a.get("name") or "").lower().endswith(".pdf") for a in noninline):
                if any(not (a.get("name") or "").lower().endswith(".pdf") for a in noninline): emit("other-attachment", received, sender, subject, "non-PDF attachment")
                else: emit("no-document", received, sender, subject, "no eligible PDF")
                record["status"]="done"
            elif saved: record["status"]="done"
            elif retry: record["status"]="open"
        state["last_date"] = today
        cutoff = datetime.date.fromisoformat(today) - datetime.timedelta(days=60)
        state["mails"] = {mid:r for mid,r in state.get("mails", {}).items() if (r.get("received", "")[:10] and datetime.date.fromisoformat(r["received"][:10]) >= cutoff) or r.get("status") == "open"}
        state["hashes"] = state.get("hashes", [])
        events.append("totals\t" + ",".join("%s=%d" % (k, totals[k]) for k in sorted(totals)))
        if not dry_run:
            os.makedirs(incoming, mode=0o700, exist_ok=True); os.chmod(incoming, 0o700)
            atomic_json(state_path, state)
            with open(log_path, "a", encoding="utf-8") as out:
                os.chmod(log_path, 0o600)
                out.write("\n".join(events) + "\n")
        return 0
    finally:
        if lock:
            lock.close()


def self_save(source, target, state, emit, received, sender, subject):
    with open(source, "rb") as f:
        digest = hashlib.sha256()
        for chunk in iter(lambda: f.read(65536), b""): digest.update(chunk)
    value = digest.hexdigest()
    if value in state.setdefault("hashes", []):
        if source != target:
            try: os.unlink(source)
            except FileNotFoundError: pass
        emit("dup", received, sender, subject, value[:12]); return False
    state["hashes"].append(value)
    if source != target: os.replace(source, target)
    os.chmod(target, 0o600)
    emit("saved", received, sender, subject, target)
    return True


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".sweep-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2); out.write("\n"); out.flush(); os.fsync(out.fileno())
        os.chmod(tmp, 0o600); os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
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
        return 2 if str(exc).startswith(("config-mode", "missing config", "already-running", "first run requires")) else 1


if __name__ == "__main__":
    sys.exit(main())

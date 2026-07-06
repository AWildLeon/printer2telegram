import imaplib
import io
import os
import smtplib
import sys
import tempfile
import time
from dataclasses import dataclass
from email.message import EmailMessage

import cups
import sane
import yaml
from imap_tools import AND, MailBox, MailMessageFlags


@dataclass
class MailAccount:
    host: str
    user: str
    password: str
    port: int


@dataclass
class Config:
    imap: MailAccount
    smtp: MailAccount
    printer: str
    scan_to: str
    allowed_emails: list[str]
    scanner: str | None = None


def load_config(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"config file not found: {path} "
                 f"(copy config.example.yaml and fill it in)")

    try:
        return Config(
            imap=MailAccount(port=993, **data["imap"]),
            smtp=MailAccount(port=465, **data["smtp"]),
            printer=data["printer"],
            scan_to=data["scan_to"],
            allowed_emails=data["allowed_emails"],
            **{k: data[k] for k in ("scanner",) if k in data},
        )
    except (KeyError, TypeError) as e:
        sys.exit(f"invalid config {path}: {e}")


def send_mail(smtp, to, subject, body, attachment=None):
    """Send a mail, optionally with one attachment.

    attachment: (filename, payload_bytes, maintype, subtype),
    e.g. ("scan.pdf", data, "application", "pdf").
    """
    msg = EmailMessage()
    msg["From"] = smtp.user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment is not None:
        filename, payload, maintype, subtype = attachment
        msg.add_attachment(payload, maintype=maintype,
                           subtype=subtype, filename=filename)
    with smtplib.SMTP_SSL(smtp.host, smtp.port) as server:
        server.login(smtp.user, smtp.password)
        server.send_message(msg)


# subject keywords (lowercase), english and german -- substring
# match, so "Bitte ausdrucken!" or "Einscannen bitte" work too
COMMANDS = {
    "print": "print",
    "drucken": "print",
    "scan": "scan",
    "scannen": "scan",
}


def classify_request(msg):
    """Decide what an incoming mail wants: "print", "scan" or None."""
    subject = msg.subject.lower()
    for keyword, action in COMMANDS.items():
        if keyword in subject:
            return action
    # no keyword, but attachments? almost certainly a print request
    if msg.attachments:
        return "print"
    return None


PRINTABLE_TYPES = ("application/pdf", "image/jpeg", "image/png")


def parse_print_options(msg):
    """Turn the mail text into CUPS job options (dict of str -> str).

    TODO(leon): parse msg.subject / msg.text for things like
      - "2x" or "2 kopien"          -> {"copies": "2"}
      - "duplex" or "beidseitig"    -> {"sides": "two-sided-long-edge"}
    Returning {} means printer defaults, so the system works
    without this -- it just ignores wishes.
    """
    return {}


def handle_print_request(msg, config):
    """Print the mail's printable attachments via CUPS."""
    options = parse_print_options(msg)
    conn = cups.Connection()
    printed = 0
    for att in msg.attachments:
        if att.content_type not in PRINTABLE_TYPES:
            print(f"skipping {att.filename} ({att.content_type})")
            continue
        suffix = os.path.splitext(att.filename)[1]
        with tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False) as f:
            f.write(att.payload)
            path = f.name
        try:
            # CUPS copies the file into its spool, so deleting
            # right after submission is safe
            conn.printFile(config.printer, path,
                           att.filename or "attachment", options)
        finally:
            os.unlink(path)
        printed += 1
    print(f"printed {printed} attachment(s) from {msg.from_}")


def parse_scan_options(msg):
    """Turn the mail text into scan settings.

    TODO(leon): parse msg.subject / msg.text for things like
      - "600dpi"                     -> {"resolution": 600}
        (regex time: re.search(r"(\\d+)\\s*dpi", ...))
      - "schwarzweiss" or "gray"     -> {"mode": "Gray"}
    Returning {} means the defaults below (300 dpi, Color).
    """
    return {}


def handle_scan_request(msg, config):
    """Scan a page and mail the result to config.scan_to."""
    options = parse_scan_options(msg)
    sane.init()
    try:
        device = config.scanner
        if device is None:
            devices = sane.get_devices()
            if not devices:
                raise RuntimeError("no scanner found")
            device = devices[0][0]
        dev = sane.open(device)
        try:
            dev.resolution = options.get("resolution", 300)
            dev.mode = options.get("mode", "Color")
            image = dev.scan()
        finally:
            dev.close()
    finally:
        sane.exit()

    buf = io.BytesIO()
    image.save(buf, "PDF", resolution=options.get("resolution", 300))
    send_mail(config.smtp, config.scan_to, "Dein Scan / Your scan",
              "Automatisch gescannt / scanned automatically.",
              ("scan.pdf", buf.getvalue(), "application", "pdf"))
    print(f"scan sent to {config.scan_to}")


def process_new_mail(mb, config):
    for msg in mb.fetch(AND(seen=False), mark_seen=False):
        handled = True
        if msg.from_ not in config.allowed_emails:
            print(f"ignoring mail from {msg.from_}")
        else:
            kind = classify_request(msg)
            print(f"request from {msg.from_}: {kind}")
            try:
                if kind == "print":
                    handle_print_request(msg, config)
                elif kind == "scan":
                    handle_scan_request(msg, config)
                else:
                    print(f"cannot classify {msg.subject!r}")
            except Exception as e:
                # leave unseen so we retry it next time
                print(f"failed to handle {msg.subject!r}: {e}")
                handled = False
        if handled:
            mb.flag(msg.uid, MailMessageFlags.SEEN, True)


def run(config):
    while True:
        try:
            imap = config.imap
            with MailBox(imap.host, imap.port).login(
                    imap.user, imap.password) as mb:
                print("connected, waiting for mail (IDLE)")
                while True:
                    # catches mail that arrived while we were away,
                    # then waits for the server to push
                    process_new_mail(mb, config)
                    mb.idle.wait(timeout=600)
        except (OSError, imaplib.IMAP4.error) as e:
            print(f"connection lost: {e}, reconnecting in 30s")
            time.sleep(30)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = load_config(path)
    print(f"config ok: printer={config.printer}, "
          f"imap={config.imap.user}@{config.imap.host}")
    run(config)


if __name__ == "__main__":
    main()

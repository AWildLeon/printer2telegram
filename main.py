import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass

import cups
import requests
import sane
import yaml


@dataclass
class Config:
    token: str
    printer: str
    allowed_users: list[int]
    admins: list[int]
    scanner: str | None = None
    state_file: str = "state.yaml"


def load_config(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"config file not found: {path} "
                 f"(copy config.example.yaml and fill it in)")

    try:
        return Config(
            token=data["telegram_token"],
            printer=data["printer"],
            allowed_users=data["allowed_users"],
            admins=data.get("admins", []),
            **{k: data[k] for k in ("scanner", "state_file")
               if k in data},
        )
    except (KeyError, TypeError) as e:
        sys.exit(f"invalid config {path}: {e}")


# per-chat defaults, adjustable via /einstellungen
DEFAULT_SETTINGS = {
    "duplex": False,      # print two-sided
    "monochrome": False,  # print in grayscale
    "media": "auto",      # paper size, "auto" = printer default
    "dpi": 300,           # scan resolution
    "gray": False,        # scan in grayscale
}

# tapping a menu row steps to the next value
SETTING_CYCLES = {
    "duplex": [False, True],
    "monochrome": [False, True],
    "media": ["auto", "A4", "A5", "Letter"],
    "dpi": [100, 200, 300, 600],
    "gray": [False, True],
}


class State:
    """Approved users and per-chat settings, kept in a yaml file."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.data = {}
        self.data.setdefault("users", [])
        self.data.setdefault("settings", {})

    def save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(self.data, f, allow_unicode=True)
        os.replace(tmp, self.path)

    def settings(self, chat_id):
        s = self.data["settings"].setdefault(chat_id, {})
        for k, v in DEFAULT_SETTINGS.items():
            s.setdefault(k, v)
        return s


def is_allowed(user, config, state):
    return (user in config.allowed_users
            or user in config.admins
            or user in state.data["users"])


class Bot:
    """Tiny Telegram Bot API client (long polling)."""

    def __init__(self, token):
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"
        self.offset = None

    def call(self, method, files=None, http_timeout=35, **params):
        r = requests.post(f"{self.api}/{method}", data=params,
                          files=files, timeout=http_timeout)
        try:
            data = r.json()
        except ValueError:
            # e.g. an HTML error page from a proxy
            r.raise_for_status()
            raise RuntimeError(f"{method}: non-JSON response")
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    def poll(self):
        """One long-poll round, returns a (possibly empty) batch."""
        updates = self.call("getUpdates", http_timeout=60,
                            offset=self.offset, timeout=50)
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates

    def send_text(self, chat_id, text):
        self.call("sendMessage", chat_id=chat_id, text=text)

    def send_document(self, chat_id, filename, payload, caption):
        self.call("sendDocument", chat_id=chat_id, caption=caption,
                  http_timeout=120,
                  files={"document": (filename, payload)})

    def download(self, file_id):
        info = self.call("getFile", file_id=file_id)
        r = requests.get(f"{self.file_api}/{info['file_path']}",
                         timeout=120)
        r.raise_for_status()
        return r.content


BOT_COMMANDS = [
    {"command": "scan",
     "description": "Scannen (Optionen: 600dpi, grau, farbe)"},
    {"command": "einstellungen",
     "description": "Standardeinstellungen für Drucken/Scannen"},
    {"command": "status",
     "description": "Drucker- und Auftragsstatus"},
    {"command": "hilfe",
     "description": "Hilfe anzeigen"},
]

HELP = ("Schick mir ein PDF oder Bild, dann drucke ich es.\n"
        "Druck-Optionen im Text: 2x, beidseitig/einseitig, "
        "schwarzweiss/farbe, a4, a5, letter\n"
        "\n"
        "/scan – scannen (Optionen: 600dpi, grau, farbe)\n"
        "/einstellungen – Standardeinstellungen ändern\n"
        "/status – Drucker- und Auftragsstatus\n"
        "/hilfe – diese Hilfe")


def bot_command(msg):
    """The leading /command, without any @botname suffix."""
    m = re.match(r"/(\w+)", msg.get("text") or "")
    return m.group(1).lower() if m else None


def classify_request(msg):
    """What a message wants: print, scan, settings, status, help."""
    # an attached file is almost certainly a print request
    if "document" in msg or "photo" in msg:
        return "print"
    cmd = bot_command(msg)
    if cmd in ("einstellungen", "settings"):
        return "settings"
    if cmd == "status":
        return "status"
    # /scan, but also plain "scannen"/"einscannen" texts
    if cmd == "scan" or re.search(r"scan", request_text(msg)):
        return "scan"
    return "help"


def request_text(msg):
    """Text + caption as one lowercase haystack for keyword parsing.

    "ss" for umlaut-s so schwarzweiss and schwarzweiß both match.
    """
    text = msg.get("text") or ""
    caption = msg.get("caption") or ""
    return f"{text} {caption}".lower().replace("ß", "ss")


def parse_print_options(msg, settings):
    """CUPS job options: the /einstellungen defaults, overridden
    by keywords in the message text.

    Understood: "2x" / "2 kopien", "beidseitig" / "einseitig",
    "schwarzweiss" / "farbe", "a4" / "a5" / "letter".
    """
    text = request_text(msg)
    options = {}
    if settings["duplex"]:
        options["sides"] = "two-sided-long-edge"
    if settings["monochrome"]:
        options["print-color-mode"] = "monochrome"
    if settings["media"] != "auto":
        options["media"] = settings["media"]
    m = re.search(r"\b(\d+)\s*(?:x\b|kopien|copies|mal\b)", text)
    if m:
        options["copies"] = m.group(1)
    if re.search(r"\b(duplex|beidseitig|doppelseitig|two.sided)\b",
                 text):
        options["sides"] = "two-sided-long-edge"
    elif re.search(r"\b(einseitig|one.sided)\b", text):
        options["sides"] = "one-sided"
    if re.search(r"\b(schwarzweiss|monochrom\w*|graustufen"
                 r"|black\s+and\s+white)\b", text):
        options["print-color-mode"] = "monochrome"
    elif re.search(r"\b(farbe|farbig|colou?r)\b", text):
        options["print-color-mode"] = "color"
    m = re.search(r"\b(a4|a5|letter)\b", text)
    if m:
        options["media"] = m.group(1).capitalize()
    return options


PRINTABLE_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def handle_print_request(bot, msg, config, state):
    """Print the message's document or photo via CUPS."""
    chat = msg["chat"]["id"]
    if "photo" in msg:
        # sizes come smallest first, take the largest
        payload = bot.download(msg["photo"][-1]["file_id"])
        name, mime = "photo.jpg", "image/jpeg"
    else:
        doc = msg["document"]
        name = doc.get("file_name") or "document"
        mime = doc.get("mime_type")
        if mime not in PRINTABLE_TYPES:
            bot.send_text(chat, f"Kann {name} ({mime}) nicht drucken "
                                f"– bitte PDF, JPEG oder PNG "
                                f"schicken.")
            return
        payload = bot.download(doc["file_id"])
    options = parse_print_options(msg, state.settings(chat))
    with tempfile.NamedTemporaryFile(
            suffix=PRINTABLE_TYPES[mime], delete=False) as f:
        f.write(payload)
        path = f.name
    try:
        # CUPS copies the file into its spool, so deleting
        # right after submission is safe
        cups.Connection().printFile(config.printer, path, name,
                                    options)
    finally:
        os.unlink(path)
    print(f"printed {name} for {msg['from']['id']}")
    bot.send_text(chat, f"Wird gedruckt: {name}")


def parse_scan_options(msg, settings):
    """Scan settings: the /einstellungen defaults, overridden by
    keywords like "600dpi", "grau" or "farbe" in the text."""
    text = request_text(msg)
    options = {"resolution": settings["dpi"]}
    if settings["gray"]:
        options["mode"] = "Gray"
    m = re.search(r"\b(\d+)\s*dpi\b", text)
    if m:
        options["resolution"] = int(m.group(1))
    if re.search(r"\b(schwarzweiss|graustufen|grau|gray|sw)\b",
                 text):
        options["mode"] = "Gray"
    elif re.search(r"\b(farbe|farbig|colou?r)\b", text):
        options["mode"] = "Color"
    return options


def pick_resolution(dev, dpi):
    """Snap dpi to something the backend allows; SANE answers
    'Invalid argument' to values outside the constraint."""
    opt = dev.opt.get("resolution")
    c = opt.constraint if opt is not None else None
    if isinstance(c, list) and c:
        return min(c, key=lambda r: abs(r - dpi))
    if isinstance(c, tuple):  # (min, max, step) range
        return min(max(dpi, c[0]), c[1])
    return dpi


def pick_mode(dev, gray):
    """Map our color/gray wish onto the backend's mode names.

    Backends disagree ("Color" vs "24bit Color", "Gray" vs
    "True Gray"), so pick from the advertised list.
    """
    opt = dev.opt.get("mode")
    c = opt.constraint if opt is not None else None
    if not isinstance(c, list) or not c:
        return "Gray" if gray else "Color"
    if gray:
        names = [n for n in c if "gray" in n.lower()]
        # prefer real grayscale over dithered variants
        names = [n for n in names if "true" in n.lower()] or names
    else:
        names = [n for n in c if "color" in n.lower()]
    return names[0] if names else c[0]


def pick_sources(dev):
    """Return (adf, flatbed) source names; either may be None."""
    opt = dev.opt.get("source")
    if opt is None or not isinstance(opt.constraint, (list, tuple)):
        return None, None
    adf = flatbed = None
    for name in opt.constraint:
        if "adf" in name.lower() or "feeder" in name.lower():
            adf = adf or name
        else:
            flatbed = flatbed or name
    return adf, flatbed


def scan_pages(dev):
    """All pages from the ADF if it holds paper, else one flatbed
    page. multi_scan() stops cleanly when the feeder runs out, so
    an empty list means 'feeder was empty from the start'."""
    adf, flatbed = pick_sources(dev)
    if adf is not None:
        dev.source = adf
        pages = list(dev.multi_scan())
        if pages:
            return pages
        if flatbed is None:
            raise RuntimeError("Dokumenteneinzug ist leer")
        dev.source = flatbed
    return [dev.scan()]


def handle_scan_request(bot, msg, config, state):
    """Scan (ADF or flatbed) and send the PDF back to the chat."""
    chat = msg["chat"]["id"]
    options = parse_scan_options(msg, state.settings(chat))
    dpi = options["resolution"]
    sane.init()
    try:
        device = config.scanner
        if device is None:
            devices = sane.get_devices()
            if not devices:
                raise RuntimeError("kein Scanner gefunden")
            device = devices[0][0]
        dev = sane.open(device)
        try:
            dpi = pick_resolution(dev, dpi)
            dev.resolution = dpi
            dev.mode = pick_mode(dev, options.get("mode") == "Gray")
            pages = scan_pages(dev)
        finally:
            dev.close()
    finally:
        sane.exit()

    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=dpi, save_all=True,
                  append_images=pages[1:])
    bot.send_document(chat, "scan.pdf", buf.getvalue(),
                      f"{len(pages)} Seite(n)")
    print(f"scan ({len(pages)} pages) sent to {chat}")


PRINTER_STATES = {3: "bereit", 4: "druckt gerade", 5: "angehalten"}


def handle_status_request(bot, jobs, msg, config):
    """Answer /status: CUPS printer state and our job queue."""
    chat = msg["chat"]["id"]
    printer = cups.Connection().getPrinters().get(config.printer, {})
    state = PRINTER_STATES.get(printer.get("printer-state"),
                               "nicht gefunden")
    bot.send_text(chat, f"Drucker: {state}\n"
                        f"Offene Aufträge: {jobs.unfinished_tasks}")


def settings_keyboard(s):
    def row(label, key):
        return [{"text": label, "callback_data": f"set:{key}"}]

    def onoff(flag):
        return "an" if flag else "aus"

    media = "Auto" if s["media"] == "auto" else s["media"]
    return {"inline_keyboard": [
        row(f"Drucken beidseitig: {onoff(s['duplex'])}", "duplex"),
        row(f"Drucken schwarzweiß: {onoff(s['monochrome'])}",
            "monochrome"),
        row(f"Papierformat: {media}", "media"),
        row(f"Scan-Auflösung: {s['dpi']} dpi", "dpi"),
        row(f"Scan in Graustufen: {onoff(s['gray'])}", "gray"),
    ]}


def handle_settings_request(bot, msg, state):
    """Answer /einstellungen with the interactive menu."""
    chat = msg["chat"]["id"]
    kb = settings_keyboard(state.settings(chat))
    bot.call("sendMessage", chat_id=chat,
             text="Einstellungen – zum Ändern antippen:",
             reply_markup=json.dumps(kb))


def handle_setting(bot, cb, state):
    """A user tapped a row in the settings menu."""
    msg = cb.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    key = (cb.get("data") or "")[4:]
    if chat is None or key not in SETTING_CYCLES:
        return
    s = state.settings(chat)
    values = SETTING_CYCLES[key]
    i = values.index(s[key]) if s[key] in values else -1
    s[key] = values[(i + 1) % len(values)]
    state.save()
    bot.call("editMessageReplyMarkup", chat_id=chat,
             message_id=msg["message_id"],
             reply_markup=json.dumps(settings_keyboard(s)))


def request_approval(bot, msg, config, pending):
    """Ask the admins whether a new user may use the bot."""
    user = msg.get("from", {})
    uid = user.get("id")
    if not config.admins or uid is None or uid in pending:
        return
    pending.add(uid)
    who = " ".join(n for n in (user.get("first_name"),
                               user.get("last_name")) if n)
    if user.get("username"):
        who = f"{who} (@{user['username']})"
    kb = {"inline_keyboard": [[
        {"text": "✅ Erlauben", "callback_data": f"allow:{uid}"},
        {"text": "❌ Ablehnen", "callback_data": f"deny:{uid}"},
    ]]}
    for admin in config.admins:
        bot.call("sendMessage", chat_id=admin,
                 text=f"{who} ({uid}) möchte den Drucker benutzen. "
                      f"Freigeben?",
                 reply_markup=json.dumps(kb))
    bot.send_text(msg["chat"]["id"],
                  "Du bist noch nicht freigeschaltet – "
                  "ich habe die Admins um Erlaubnis gefragt.")


def handle_approval(bot, cb, config, state):
    """An admin pressed Erlauben/Ablehnen."""
    if cb["from"]["id"] not in config.admins:
        return "Nur für Admins."
    action, _, uid = (cb.get("data") or "").partition(":")
    uid = int(uid)
    if action == "allow":
        verdict = "✅ freigegeben"
        if uid not in state.data["users"]:
            state.data["users"].append(uid)
            state.save()
            bot.send_text(uid, "Du bist freigeschaltet! "
                               "/hilfe zeigt, was ich kann.")
    else:
        verdict = "❌ abgelehnt"
        bot.send_text(uid, "Deine Anfrage wurde abgelehnt.")
    msg = cb.get("message")
    if msg:
        bot.call("editMessageText", chat_id=msg["chat"]["id"],
                 message_id=msg["message_id"],
                 text=f"{msg.get('text', '')}\n{verdict}")
    return verdict


def handle_callback(bot, cb, config, state):
    """Route a button press (settings menu or admin approval)."""
    data = cb.get("data") or ""
    answer = None
    try:
        if data.startswith(("allow:", "deny:")):
            answer = handle_approval(bot, cb, config, state)
        elif (data.startswith("set:")
              and is_allowed(cb["from"]["id"], config, state)):
            handle_setting(bot, cb, state)
    except Exception:
        traceback.print_exc()
    finally:
        # always stop the button's loading spinner; this fails
        # ("query is too old") for buttons that were pressed
        # while we were offline, which is fine
        try:
            bot.call("answerCallbackQuery",
                     callback_query_id=cb["id"], text=answer)
        except RuntimeError as e:
            print(f"cannot answer callback: {e}")


# wait this long between attempts while the device is busy
RETRY_DELAYS = [15, 30, 60, 120, 300]


def is_busy_error(e):
    return "busy" in str(e).lower()


def run_job(bot, chat, job):
    """Run one queued job, waiting out a busy device with backoff."""
    for i in range(len(RETRY_DELAYS) + 1):
        try:
            job()
            return
        except Exception as e:
            if i == len(RETRY_DELAYS) or not is_busy_error(e):
                traceback.print_exc()
                bot.send_text(chat, f"Fehlgeschlagen: {e}")
                return
            if i == 0:
                bot.send_text(chat, "Das Gerät ist gerade "
                                    "beschäftigt – ich versuche es "
                                    "automatisch weiter.")
            print(f"device busy, retrying in {RETRY_DELAYS[i]}s")
            time.sleep(RETRY_DELAYS[i])


def worker(bot, jobs):
    """Process print/scan jobs one after the other, forever."""
    while True:
        chat, job = jobs.get()
        try:
            run_job(bot, chat, job)
        except Exception:
            # e.g. telegram unreachable while reporting a failure
            traceback.print_exc()
        finally:
            jobs.task_done()


def enqueue(bot, jobs, chat, note, job):
    """Queue a job; tell the user if it has to wait its turn."""
    if jobs.unfinished_tasks:
        bot.send_text(chat, "Ein anderer Auftrag läuft noch – "
                            "deiner ist eingereiht.")
    elif note:
        bot.send_text(chat, note)
    jobs.put((chat, job))


def handle_message(bot, jobs, msg, config, state, pending):
    user = msg.get("from", {}).get("id")
    chat = msg["chat"]["id"]
    try:
        if not is_allowed(user, config, state):
            # this id is what an admin approval would add
            print(f"unknown user {user}, asking admins")
            request_approval(bot, msg, config, pending)
            return
        kind = classify_request(msg)
        print(f"request from {user}: {kind}")
        if kind == "print":
            enqueue(bot, jobs, chat, None,
                    lambda: handle_print_request(bot, msg, config,
                                                 state))
        elif kind == "scan":
            enqueue(bot, jobs, chat, "Scanne …",
                    lambda: handle_scan_request(bot, msg, config,
                                                state))
        elif kind == "settings":
            handle_settings_request(bot, msg, state)
        elif kind == "status":
            handle_status_request(bot, jobs, msg, config)
        else:
            bot.send_text(chat, HELP)
    except Exception as e:
        print(f"failed to handle message from {user}:")
        traceback.print_exc()
        bot.send_text(chat, f"Fehlgeschlagen: {e}")


def run(config):
    bot = Bot(config.token)
    state = State(config.state_file)
    jobs = queue.Queue()
    pending = set()
    threading.Thread(target=worker, args=(bot, jobs),
                     daemon=True).start()
    while True:
        try:
            me = bot.call("getMe")
            bot.call("setMyCommands",
                     commands=json.dumps(BOT_COMMANDS))
            print(f"connected as @{me['username']}, polling")
            while True:
                for update in bot.poll():
                    # one broken update must not kill the daemon
                    # (e.g. replying to a user who blocked the bot)
                    try:
                        msg = update.get("message")
                        if msg is not None:
                            handle_message(bot, jobs, msg, config,
                                           state, pending)
                        cb = update.get("callback_query")
                        if cb is not None:
                            handle_callback(bot, cb, config, state)
                    except Exception:
                        traceback.print_exc()
        except (OSError, requests.RequestException) as e:
            print(f"connection lost: {e}, reconnecting in 30s")
            time.sleep(30)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = load_config(path)
    print(f"config ok: printer={config.printer}, "
          f"{len(config.allowed_users)} allowed user(s), "
          f"{len(config.admins)} admin(s)")
    run(config)


if __name__ == "__main__":
    main()

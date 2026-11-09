# actions/email_agent.py
#
# Connects to the user's own mailbox over IMAP/SMTP (an app password, not
# their real password — required by Gmail/Outlook/Yahoo for exactly this
# kind of third-party access) rather than Google OAuth. That's a deliberate
# choice: a real OAuth integration needs a registered cloud app, a consent
# screen, and a review process — none of which can be set up from inside
# this codebase. IMAP/SMTP with an app password is the version of this
# feature that's actually usable the moment you fill in the config file.
#
# Default behavior is DRAFT ONLY — composed emails are saved to the
# Drafts folder via IMAP APPEND, never sent automatically. That's what
# was asked for ("put it in drafts so I can send them when I read
# through them"), and it's also just the safer default for anything an
# AI writes on your behalf.

import imaplib
import smtplib
import ssl
import sys
import json
import re
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from email.header import decode_header


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _get_base_dir() / "config" / "email_config.json"

_KNOWN_PROVIDERS = {
    "gmail.com":      {"imap": "imap.gmail.com",        "imap_port": 993, "smtp": "smtp.gmail.com",         "smtp_port": 587},
    "googlemail.com": {"imap": "imap.gmail.com",        "imap_port": 993, "smtp": "smtp.gmail.com",         "smtp_port": 587},
    "outlook.com":    {"imap": "outlook.office365.com", "imap_port": 993, "smtp": "smtp.office365.com",     "smtp_port": 587},
    "hotmail.com":    {"imap": "outlook.office365.com", "imap_port": 993, "smtp": "smtp.office365.com",     "smtp_port": 587},
    "live.com":       {"imap": "outlook.office365.com", "imap_port": 993, "smtp": "smtp.office365.com",     "smtp_port": 587},
    "yahoo.com":      {"imap": "imap.mail.yahoo.com",   "imap_port": 993, "smtp": "smtp.mail.yahoo.com",    "smtp_port": 587},
    "icloud.com":     {"imap": "imap.mail.me.com",      "imap_port": 993, "smtp": "smtp.mail.me.com",       "smtp_port": 587},
    "me.com":         {"imap": "imap.mail.me.com",      "imap_port": 993, "smtp": "smtp.mail.me.com",       "smtp_port": 587},
}

_SETUP_MESSAGE = (
    "I'm not connected to an email account yet, sir. Create "
    "config/email_config.json with:\n"
    '{\n'
    '  "email": "you@gmail.com",\n'
    '  "app_password": "xxxx xxxx xxxx xxxx"\n'
    '}\n'
    "For Gmail: turn on 2-Step Verification, then create an App Password "
    "at myaccount.google.com/apppasswords — use that, not your normal "
    "password. Outlook/Yahoo/iCloud work the same way with their own "
    "app-password pages. Restart me after saving the file."
)


def _load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not cfg.get("email") or not cfg.get("app_password"):
        return None
    # Google displays app passwords as "abcd efgh ijkl mnop" — the spaces
    # aren't part of the password.
    cfg["app_password"] = str(cfg["app_password"]).replace(" ", "").strip()
    cfg["email"] = str(cfg["email"]).strip()

    domain = cfg["email"].split("@")[-1].lower()
    provider = _KNOWN_PROVIDERS.get(domain, {})
    cfg.setdefault("imap_host", provider.get("imap"))
    cfg.setdefault("imap_port", provider.get("imap_port", 993))
    cfg.setdefault("smtp_host", provider.get("smtp"))
    cfg.setdefault("smtp_port", provider.get("smtp_port", 587))

    if not cfg["imap_host"] or not cfg["smtp_host"]:
        return None
    return cfg


def _connect_imap(cfg: dict) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    imap.login(cfg["email"], cfg["app_password"])
    return imap


def _imap_select(imap: imaplib.IMAP4_SSL, mailbox: str, readonly: bool = True):
    """Wraps imap.select() adding double-quotes around mailbox names that
    contain spaces or brackets — without them, Gmail returns BAD 'Could not
    parse command' for names like [Gmail]/Sent Mail."""
    needs_quoting = any(c in mailbox for c in (' ', '[', ']', '/'))
    quoted = f'"{mailbox}"' if needs_quoting and not mailbox.startswith('"') else mailbox
    return imap.select(quoted, readonly=readonly)


def _find_mailbox(imap: imaplib.IMAP4_SSL, keyword: str, fallbacks: list[str]) -> str | None:
    """Finds the real mailbox name for e.g. 'Sent' or 'Drafts' — these
    vary by provider/locale (Gmail uses "[Gmail]/Sent Mail", others use
    plain "Sent"), so we list what's actually there instead of guessing."""
    try:
        status, boxes = imap.list()
        if status == "OK":
            for raw in boxes:
                name = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                match = re.search(r'"([^"]+)"\s*$', name)
                mailbox = match.group(1) if match else name.split()[-1]
                if keyword.lower() in mailbox.lower():
                    return mailbox
    except Exception:
        pass
    for fb in fallbacks:
        try:
            status, _ = _imap_select(imap, fb, readonly=True)
            if status == "OK":
                return fb
        except Exception:
            continue
    return None


def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out)


def _plain_text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return ""


def _strip_quoted(text: str) -> str:
    """Cuts off quoted reply chains ('On ... wrote:', '> quoted line')
    so style samples are the user's own words, not whoever they replied to."""
    lines = text.splitlines()
    out = []
    for line in lines:
        if re.match(r"^\s*On .+wrote:\s*$", line):
            break
        if line.strip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _get_style_samples(imap: imaplib.IMAP4_SSL, count: int = 12) -> list[str]:
    import email as email_lib

    sent_box = _find_mailbox(imap, "sent", ["[Gmail]/Sent Mail", "Sent", "Sent Items", "INBOX.Sent"])
    if not sent_box:
        return []

    status, _ = _imap_select(imap, sent_box, readonly=True)
    if status != "OK":
        return []

    status, data = imap.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []

    ids = data[0].split()[-count:]
    samples = []
    for msg_id in reversed(ids):
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1]
        msg = email_lib.message_from_bytes(raw)
        body = _strip_quoted(_plain_text_body(msg)).strip()
        if 20 < len(body) < 3000:
            samples.append(body)
    return samples


def _compose_styled_email(topic: str, to: str, samples: list[str]) -> tuple[str, str]:
    from or_client import client
    import re as _re

    # Extract explicit word count if the user specified one
    wc_match = _re.search(r"(\d+)\s*words?", topic, _re.IGNORECASE)
    word_count = int(wc_match.group(1)) if wc_match else None
    length_instruction = (
        f"The email body MUST be approximately {word_count} words — count carefully and reach that target."
        if word_count else
        "Write a complete, substantive email with at least 3 paragraphs."
    )

    if samples:
        examples = "\n\n---\n\n".join(samples[:8])
        style_block = (
            f"Here are real emails this person has actually sent, for style reference only "
            f"(do not reuse their content, only match tone/vocabulary/sentence length/sign-off):\n\n"
            f"{examples[:6000]}"
        )
    else:
        style_block = "No writing samples available — use a natural, friendly, professional default tone."

    prompt = (
        f"{style_block}\n\n"
        f"---\n\n"
        f"Write a new email to: {to or '(recipient not specified)'}\n"
        f"Topic / subject: {topic}\n\n"
        f"IMPORTANT: {length_instruction}\n"
        f"Write the COMPLETE email body — do not summarise or describe what you would write, "
        f"actually write it in full.\n\n"
        f"Return in EXACTLY this format, nothing else:\n"
        f"SUBJECT: <subject line>\n"
        f"BODY:\n<complete email body>"
    )

    raw = client.chat(
        prompt,
        system=(
            "You write complete, full-length emails that sound exactly like the person whose "
            "samples you were shown — same tone, vocabulary, sentence length, greeting, and "
            "sign-off style. NEVER summarise. NEVER say 'this email covers X'. "
            "Write the actual content in full. Never mention that you're an AI."
        ),
        max_tokens=2000,
    ).strip()

    subject, body = "New message", raw
    m = re.search(r"SUBJECT:\s*(.+?)\nBODY:\s*(.*)", raw, re.DOTALL)
    if m:
        subject = m.group(1).strip()
        body    = m.group(2).strip()
    return subject, body


def _save_draft(imap: imaplib.IMAP4_SSL, cfg: dict, to: str, subject: str, body: str) -> str:
    drafts_box = _find_mailbox(imap, "draft", ["[Gmail]/Drafts", "Drafts", "INBOX.Drafts"])
    if not drafts_box:
        return "Could not find a Drafts folder on this account."

    msg = MIMEMultipart()
    msg["From"] = cfg["email"]
    msg["To"] = to or ""
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.attach(MIMEText(body, "plain"))

    needs_q = any(c in drafts_box for c in (" ","[","]","/")) and not drafts_box.startswith(chr(34))
    qbox = chr(34)+drafts_box+chr(34) if needs_q else drafts_box
    status, _ = imap.append(qbox, "\\Draft", imaplib.Time2Internaldate(__import__("time").time()), msg.as_bytes())
    if status != "OK":
        return f"Could not save to {drafts_box}."
    return f"Saved to {drafts_box}."


def _send_now(cfg: dict, to: str, subject: str, body: str) -> str:
    msg = MIMEMultipart()
    msg["From"] = cfg["email"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
        server.starttls(context=context)
        server.login(cfg["email"], cfg["app_password"])
        server.sendmail(cfg["email"], [to], msg.as_string())
    return f"Sent to {to}."


def _read_recent(imap: imaplib.IMAP4_SSL, count: int = 8) -> str:
    status, _ = _imap_select(imap, "INBOX", readonly=True)
    if status != "OK":
        return "Could not open the inbox."
    status, data = imap.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        return "Inbox is empty."
    ids = data[0].split()[-count:]
    lines = []
    for msg_id in reversed(ids):
        status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        header_bytes = msg_data[0][1]
        header_text = header_bytes.decode(errors="ignore")
        frm = re.search(r"From:\s*(.+)", header_text)
        subj = re.search(r"Subject:\s*(.+)", header_text)
        lines.append(f"- {_decode(subj.group(1)) if subj else '(no subject)'} — {_decode(frm.group(1)) if frm else '?'}")
    return "\n".join(lines) if lines else "No recent messages found."


def _find_latest_from(imap: imaplib.IMAP4_SSL, who: str):
    """Latest inbox email whose sender (or subject) matches `who`."""
    import email as email_lib
    status, _ = _imap_select(imap, "INBOX", readonly=True)
    if status != "OK":
        return None
    crit = f'(OR FROM "{who}" SUBJECT "{who}")' if who else "ALL"
    status, data = imap.search(None, crit)
    if status != "OK" or not data or not data[0]:
        return None
    msg_id = data[0].split()[-1]
    status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")
    if status != "OK" or not msg_data or not msg_data[0]:
        return None
    return email_lib.message_from_bytes(msg_data[0][1])


def _open_gmail_compose(to: str, subject: str, body: str) -> str:
    """No mailbox connected → open a pre-filled Gmail compose window so the
    user just reviews and presses Send."""
    from urllib.parse import quote
    url = (f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(to or '')}"
           f"&su={quote(subject or '')}&body={quote(body or '')}")
    try:
        from actions.browser_util import open_url
        open_url(url)
    except Exception:
        import webbrowser
        webbrowser.open(url)
    try:
        import pyperclip
        pyperclip.copy(body)
    except Exception:
        pass
    return "Opened it in a Gmail compose window (body also copied to the clipboard) — review it and press Send."


def email_agent(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action   : draft (default) | send | reply | read_recent | learn_style
        to       : recipient address (or, for reply, who sent the email being answered)
        topic    : what the email should say — the user's own words are fine
        tone     : optional (formal, friendly, apologetic, …)
        language : optional (English, German, …) — auto-detected otherwise
        subject / body : literal overrides (skip the writer)
    """
    from actions.smart_writer import compose_email

    p        = parameters or {}
    action   = str(p.get("action", "draft")).lower().strip()
    to       = str(p.get("to", "")).strip()
    topic    = str(p.get("topic", "")).strip()
    tone     = str(p.get("tone", "")).strip()
    language = str(p.get("language", "")).strip()
    cfg      = _load_config()

    if player:
        player.write_log(f"[Email] {action}")

    if action in ("read_recent", "learn_style", "reply") and not cfg:
        return ("That needs your mailbox connected: open Settings (Ctrl+,) and add your email "
                "and a Gmail app password (myaccount.google.com/apppasswords). Writing new "
                "emails works without it — they open ready-to-send in Gmail.")

    # ── no mailbox: write it and open a ready Gmail compose window ──────
    if not cfg:
        if p.get("body"):
            subject, body = p.get("subject") or "", p["body"]
        else:
            if not topic:
                return "What should the email say?"
            mail = compose_email(topic, to=to, tone=tone, language=language)
            subject, body = p.get("subject") or mail.get("subject", ""), mail.get("body", "")
        status = _open_gmail_compose(to, subject, body)
        return f"SUBJECT: {subject}\n\n{body}\n\n({status})"

    try:
        imap = _connect_imap(cfg)
    except Exception as e:
        return f"Could not connect to your mailbox: {e}"

    try:
        if action == "read_recent":
            return _read_recent(imap, int(p.get("count", 8)))

        if action == "learn_style":
            samples = _get_style_samples(imap, count=int(p.get("count", 15)))
            return (f"Learned from {len(samples)} of your sent emails."
                    if samples else "Couldn't find any sent emails to learn from.")

        thread, reply_to_msg = "", None
        if action == "reply":
            reply_to_msg = _find_latest_from(imap, to)
            if reply_to_msg is None:
                return f"Couldn't find an email from '{to}' in your inbox."
            sender  = _decode(reply_to_msg.get("From", ""))
            subj    = _decode(reply_to_msg.get("Subject", ""))
            thread  = f"From: {sender}\nSubject: {subj}\n\n{_strip_quoted(_plain_text_body(reply_to_msg))[:4000]}"
            m = re.search(r"<([^>]+)>", sender)
            to = m.group(1) if m else sender

        if p.get("body"):
            subject, body = p.get("subject") or "New message", p["body"]
        else:
            if not topic and action != "reply":
                return "What should the email say?"
            samples = _get_style_samples(imap)
            mail = compose_email(topic or "write a fitting reply", to=to, tone=tone,
                                 language=language, thread=thread, samples=samples)
            subject = p.get("subject") or mail.get("subject") or "New message"
            body    = mail.get("body", "")

        if action == "send":
            if not to:
                return "I need a recipient address to send it."
            result = _send_now(cfg, to, subject, body)
        else:   # draft + reply → saved as a draft; the user sends it
            result = _save_draft(imap, cfg, to, subject, body)
        return f"SUBJECT: {subject}\nTO: {to}\n\n{body}\n\n({result})"

    except Exception as e:
        return f"Email action failed: {e}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass

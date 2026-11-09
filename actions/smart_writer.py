# actions/smart_writer.py
#
# One writer for everything BRINKO writes in the user's name — emails and
# chat messages. What makes it "smart" instead of generic:
#   • works out the real goal from a loose request ("tell my boss I'm sick"),
#   • picks the right language (German for German contacts/requests, etc.),
#   • matches the relationship: boss ≠ mum ≠ best friend,
#   • right LENGTH for the medium: texts are short and human, emails are as
#     long as they need to be — never padded,
#   • copies the user's own style when real samples exist,
#   • never invents facts (dates, prices, reasons) the user didn't give —
#     it leaves a clear [placeholder] instead.

from __future__ import annotations

import json
import re

from memory.memory_manager import load_memory


def _user_profile() -> str:
    try:
        mem = load_memory()
    except Exception:
        return ""
    lines = []
    for cat in ("identity", "relationships", "preferences", "notes"):
        for k, v in (mem.get(cat) or {}).items():
            val = v.get("value") if isinstance(v, dict) else v
            if val:
                lines.append(f"{cat}/{k}: {val}")
    return "\n".join(lines[:40])


_COMMON_RULES = (
    "Rules:\n"
    "- Write as the user, in first person. Never mention AI, assistants, or that this was generated.\n"
    "- Do NOT invent facts the user didn't give (times, dates, prices, names, reasons, promises). "
    "If something essential is missing, put a short [placeholder] in square brackets.\n"
    "- Language: use the language the user asked in, unless they asked for another or the recipient "
    "clearly speaks something else (e.g. a German name/company/context → German). For German, use "
    "'Sie' for formal recipients (boss, company, teacher, authority, unknown people) and 'du' for "
    "friends and family.\n"
    "- Sound like a real, likeable person — natural, specific, no clichés like 'I hope this email finds you well'.\n"
)


def compose_email(request: str, to: str = "", tone: str = "", language: str = "",
                  thread: str = "", samples: list[str] | None = None) -> dict:
    """Returns {'subject','body','language'}."""
    from or_client import client

    style = ""
    if samples:
        style = ("Emails this person really sent (match their tone, greeting, sign-off, sentence length; "
                 "do NOT reuse their content):\n---\n" + "\n---\n".join(s[:1200] for s in samples[:6]) + "\n---\n")
    wc = re.search(r"(\d+)\s*words?", request, re.I)
    length = (f"Body length: about {wc.group(1)} words." if wc else
              "Length: exactly as long as it needs to be — usually 3–8 sentences. Short is good.")
    profile = _user_profile()
    prompt = (
        f"{style}"
        f"{'What we know about the user:' + chr(10) + profile + chr(10) if profile else ''}"
        f"Write an email.\n"
        f"Recipient: {to or 'not specified'}\n"
        f"What the user wants: {request}\n"
        f"{'Tone: ' + tone + chr(10) if tone else ''}"
        f"{'Language: ' + language + chr(10) if language else ''}"
        f"{'This is a REPLY. The email being answered:' + chr(10) + thread[:4000] + chr(10) if thread else ''}"
        f"{length}\n\n{_COMMON_RULES}"
        "- Fitting greeting and sign-off for the relationship. Sign with the user's first name if known, "
        "otherwise [Your name].\n"
        "- Subject: short and specific (for a reply: 'Re: ' + original subject).\n\n"
        'Return ONLY JSON: {"subject": "...", "body": "...", "language": "en|de|..."}'
    )
    raw = client.chat(prompt, system="You are an excellent personal ghostwriter for emails.",
                      max_tokens=4000, temperature=0.6, smart=True)
    return _parse(raw, default_subject="Hello")


def compose_text(request: str, recipient: str = "", platform: str = "WhatsApp",
                 tone: str = "", language: str = "") -> dict:
    """Returns {'text','language'} — a chat message, not an email."""
    from or_client import client

    profile = _user_profile()
    prompt = (
        f"{'What we know about the user:' + chr(10) + profile + chr(10) if profile else ''}"
        f"Write a {platform} message.\n"
        f"To: {recipient or 'not specified'}\n"
        f"What the user wants to say: {request}\n"
        f"{'Tone: ' + tone + chr(10) if tone else ''}"
        f"{'Language: ' + language + chr(10) if language else ''}\n"
        f"{_COMMON_RULES}"
        "- It's a TEXT: short (usually 1–3 sentences), casual punctuation is fine, no subject, "
        "no 'Dear', no sign-off/signature. Emoji only if it fits the relationship, max one or two.\n\n"
        'Return ONLY JSON: {"text": "...", "language": "en|de|..."}'
    )
    raw = client.chat(prompt, system="You write natural, human chat messages for the user.",
                      max_tokens=2500, temperature=0.7, smart=True)
    out = _parse(raw)
    if not out.get("text"):
        out["text"] = out.get("body") or raw.strip().strip('"')
    return out


def _parse(raw: str, default_subject: str = "") -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        clean = clean[4:] if clean.startswith("json") else clean
    m = re.search(r"\{.*\}", clean, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if default_subject:
                data.setdefault("subject", default_subject)
            return data
        except json.JSONDecodeError:
            pass
    return {"subject": default_subject, "body": clean, "text": clean}

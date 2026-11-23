# inbox.py — messages + emails in one place.
#
#  • MESSAGES: reads Windows toast notifications (UserNotificationListener, the same pop-ups the user
#    sees) from messaging apps — WhatsApp, Telegram, Signal, Messenger, Instagram, Phone Link (SMS),
#    Discord DMs — and has BRINKO read each new one aloud. Notifications already in the Action
#    Center when BRINKO starts = "missed while you were away" (startup briefing).
#  • EMAILS: polls EVERY saved mailbox (email_agent.all_configs) every 60 s; mail from real people is
#    read out (sender, subject, gist); newsletters/shops are only listed.
#  • startup_digest(): missed messages + unread mail since the last session + headlines, for the
#    greeting; the inbox window (fx/inbox.html via /fx/api/inbox) shows all of it.
#  Stored in memory/messages.json, memory/emails.json, config/inbox_state.json (all git-ignored).

import asyncio
import email
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MSG_FILE = BASE / "memory" / "messages.json"
MAIL_FILE = BASE / "memory" / "emails.json"
STATE = BASE / "config" / "inbox_state.json"

MSG_APPS = ("whatsapp", "telegram", "signal", "messenger", "instagram", "phone link", "smartphone-link",
            "discord", "teams", "skype", "imessage", "line", "viber", "threema")
_lock = threading.Lock()
_started = False
_brinko = None
_ui = None
_news_cache: tuple[float, list] = (0.0, [])


# ── small storage helpers ───────────────────────────────────────────────────
def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def _state() -> dict:
    return _load(STATE, {})


def _set_state(**kw):
    with _lock:
        st = _state()
        st.update(kw)
        _save(STATE, st)


def _add(path: Path, item: dict, cap: int):
    with _lock:
        items = _load(path, [])
        items.append(item)
        _save(path, items[-cap:])


def _user_name() -> str:
    try:
        from memory.memory_manager import load_memory
        v = (load_memory().get("identity") or {}).get("name")
        return (v.get("value") if isinstance(v, dict) else v) or ""
    except Exception:
        return ""


def _say(msg: str):
    """Through Proactive so it never talks over the user or itself (urgent = also in standby)."""
    p = getattr(_brinko, "proactive", None)
    if p:
        p.notify(msg, urgent=True)
    elif _brinko:
        _brinko.speak(msg)


# ── MESSAGES (Windows notifications) ────────────────────────────────────────
def _toast(n):
    from winrt.windows.ui.notifications import KnownNotificationBindings
    try:
        app = n.app_info.display_info.display_name
    except Exception:
        app = ""
    b = n.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
    texts = [t.text for t in b.get_text_elements()] if b else []
    texts = [re.sub(r"[⁦-⁩‎‏]", "", t).strip() for t in texts if t and t.strip()]
    try:
        ts = n.creation_time.timestamp() if hasattr(n.creation_time, "timestamp") else time.time()
    except Exception:
        ts = time.time()
    return {"id": n.id, "app": app, "texts": texts, "t": ts}


def _as_message(tst: dict) -> dict | None:
    app = tst["app"]
    if not app or not any(a in app.lower() for a in MSG_APPS) or not tst["texts"]:
        return None
    sender = tst["texts"][0]
    body = " ".join(tst["texts"][1:]).strip() or sender
    if "discord" in app.lower() and "#" in sender:          # server channels, not DMs
        return None
    sender = re.sub(r"\s*\(\d+ (new )?messages?\)$", "", sender).strip()
    return {"app": app, "from": sender[:80], "text": body[:600], "t": tst["t"],
            "time": datetime.fromtimestamp(tst["t"]).strftime("%H:%M")}


async def _list_toasts():
    from winrt.windows.ui.notifications.management import UserNotificationListener, UserNotificationListenerAccessStatus
    from winrt.windows.ui.notifications import NotificationKinds
    lst = UserNotificationListener.current
    if await lst.request_access_async() != UserNotificationListenerAccessStatus.ALLOWED:
        return None
    return [_toast(n) for n in await lst.get_notifications_async(NotificationKinds.TOAST)]


_first_pass = threading.Event()
_missed: list[dict] = []


def missed_messages(wait: float = 8.0) -> list[dict]:
    """Messaging notifications that were in the Action Center when BRINKO started. Only the watcher
    thread touches WinRT (two threads calling the notification listener at once hung the greeting)."""
    if not _started:
        try:
            toasts = asyncio.run(_list_toasts()) or []
        except Exception as e:
            print(f"[Inbox] notifications: {e}")
            return []
        return [m for m in (_as_message(t) for t in toasts) if m]
    _first_pass.wait(wait)
    return list(_missed)


def _message_loop():
    async def run():
        seen = None
        while True:
            try:
                toasts = await _list_toasts()
                if toasts is None:
                    _first_pass.set()
                    print("[Inbox] ⚠️ Windows notification access is off (Settings > Privacy > Notifications)")
                    await asyncio.sleep(60)
                    continue
                ids = {t["id"] for t in toasts}
                if seen is None:                       # first pass: these are the "missed" ones
                    seen = ids
                    for m in (_as_message(t) for t in toasts):
                        if m:
                            _missed.append(m)
                            _add(MSG_FILE, {**m, "missed": True}, 150)
                    _first_pass.set()
                else:
                    for t in toasts:
                        if t["id"] in seen:
                            continue
                        m = _as_message(t)
                        if m:
                            _add(MSG_FILE, m, 150)
                            if _ui:
                                _ui.write_log(f"SYS: 💬 {m['app']} — {m['from']}: {m['text'][:60]}")
                            app = m["app"].split()[0]
                            nm = _user_name()
                            _say(f"[NEW MESSAGE — read it out now] {app} message from {m['from']}: \"{m['text']}\". "
                                 f"Say it naturally, e.g. '{nm + ', ' if nm else ''}you just got a {app} message from "
                                 f"{m['from']}. It says: …' and read the text as written (its own language). If it's "
                                 f"very long, read the first two sentences and summarise the rest. No tools.")
                    seen = ids | (seen if len(seen) < 500 else set())
            except Exception as e:
                _first_pass.set()
                print(f"[Inbox] messages: {str(e)[:120]}")
            await asyncio.sleep(2)
    try:
        asyncio.run(run())
    except Exception as e:
        print(f"[Inbox] message watcher stopped: {e}")


# ── EMAILS (every saved mailbox) ────────────────────────────────────────────
def _fetch_new(cfg: dict, since_uid: int | None, limit: int = 8):
    """New UNSEEN mail after since_uid in one mailbox → (items, newest uid). since_uid None = baseline only."""
    from actions import email_agent as E
    from actions.proactive import _is_person
    imap = E._connect_imap(cfg)
    try:
        E._imap_select(imap, "INBOX", readonly=True)
        st, data = imap.uid("search", None, "UNSEEN")
        uids = [int(x) for x in (data[0].split() if st == "OK" and data and data[0] else [])]
        st2, d2 = imap.uid("search", None, "ALL")
        all_uids = [int(x) for x in (d2[0].split() if st2 == "OK" and d2 and d2[0] else [])]
        newest = max(all_uids) if all_uids else (since_uid or 0)
        if since_uid is None:
            return [], newest
        out = []
        for uid in [u for u in uids if u > since_uid][-limit:]:
            st, md = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            if st != "OK" or not md or not md[0]:
                continue
            raw = md[0][1]
            msg = email.message_from_bytes(raw)
            frm = E._decode(msg.get("From", ""))
            hdrs = raw[:6000].decode(errors="ignore")
            body = re.sub(r"\s+", " ", E._strip_quoted(E._plain_text_body(msg) or ""))[:700]
            out.append({"account": cfg["email"], "uid": uid, "from": re.sub(r"\s*<.*?>", "", frm).strip().strip('"') or frm,
                        "address": (re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", frm) or [""])[0] if frm else "",
                        "subject": E._decode(msg.get("Subject", "")) or "(no subject)", "snippet": body,
                        "person": _is_person(frm, hdrs), "t": time.time(),
                        "time": datetime.now().strftime("%H:%M")})
        return out, newest
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def unread_since_last_time() -> list[dict]:
    """Startup: unread mail that arrived since BRINKO last checked each mailbox."""
    from actions import email_agent as E
    st = _state().get("mail_uid", {})
    found = []
    for cfg in E.all_configs():
        try:
            last = st.get(cfg["email"])
            if last is None:                             # first ever run: show today's unread
                items, newest = _fetch_new(cfg, 0, limit=6)
                items = [i for i in items if i["person"]] or items[-3:]
            else:
                items, newest = _fetch_new(cfg, int(last), limit=10)
            for i in items:
                _add(MAIL_FILE, {**i, "missed": True}, 120)
            found += items
            st[cfg["email"]] = newest
        except Exception as e:
            print(f"[Inbox] {cfg['email']}: {str(e)[:100]}")
    _set_state(mail_uid=st)
    return found


def _mail_loop():
    from actions import email_agent as E
    time.sleep(20)                                       # let the startup digest take its baseline first
    while True:
        st = _state().get("mail_uid", {})
        for cfg in E.all_configs():
            try:
                items, newest = _fetch_new(cfg, st.get(cfg["email"]))
                st[cfg["email"]] = max(newest, int(st.get(cfg["email"]) or 0))
                for i in items:
                    _add(MAIL_FILE, i, 120)
                    if _ui:
                        _ui.write_log(f"SYS: ✉ {cfg['email'].split('@')[0]} — {i['from']}: {i['subject'][:50]}")
                people = [i for i in items if i["person"]]
                for i in people[:3]:
                    multi = len(E.all_configs()) > 1
                    _say(f"[NEW EMAIL — tell the user now] From {i['from']} ({i['address']})"
                         f"{' to ' + cfg['email'] if multi else ''}, subject \"{i['subject']}\". Text: {i['snippet'][:400]}. "
                         f"Say who it's from{', which inbox' if multi else ''} and what it's about in 1-2 sentences, "
                         f"then offer to read it fully or reply. No tools.")
                others = len(items) - len(people)
                if others and not people and _ui:
                    _ui.write_log(f"SYS: ✉ {others} new newsletter/notification email(s) in {cfg['email']}")
            except Exception as e:
                print(f"[Inbox] mail {cfg.get('email')}: {str(e)[:100]}")
        _set_state(mail_uid=st)
        time.sleep(60)


# ── news + digest ───────────────────────────────────────────────────────────
def headlines(n: int = 5) -> list[dict]:
    global _news_cache
    if time.time() - _news_cache[0] < 1800 and _news_cache[1]:
        return _news_cache[1]
    out = []
    try:
        from actions.web_search import _ddg_news
        seen = set()
        for q in ("top world news today", "breaking news", "technology news today"):
            for r in _ddg_news(q, max_results=4):
                k = r.get("title", "")[:60].lower()
                if " | " in r.get("title", "") or "top stories" in k or len(k.split()) < 5:
                    continue
                if k and k not in seen:
                    seen.add(k)
                    out.append({"title": r.get("title", ""), "source": r.get("source", ""),
                                "date": str(r.get("date", ""))[:16], "url": r.get("url", "")})
            if len(out) >= n:
                break
    except Exception as e:
        print(f"[Inbox] news: {e}")
    _news_cache = (time.time(), out[:n])
    return out[:n]


def startup_digest() -> dict:
    """Facts for the greeting; also fills the stores the inbox window shows."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(3) as ex:
        fm, fe, fn = ex.submit(missed_messages), ex.submit(unread_since_last_time), ex.submit(headlines, 4)
        msgs = fm.result(timeout=20) if fm else []
        mails = fe.result(timeout=25)
        news = fn.result(timeout=20)
    print(f"[Inbox] startup digest: {len(msgs)} messages, {len(mails)} emails, {len(news)} headlines")
    return {"messages": msgs, "emails": mails, "news": news}


def digest_text(d: dict) -> str:
    parts = []
    m = d.get("messages") or []
    if m:
        by = {}
        for x in m:
            by.setdefault(f"{x['from']} ({x['app'].split()[0]})", []).append(x["text"])
        parts.append("Messages they missed: " + "; ".join(f"{k}: \"{' / '.join(v)[:140]}\"" for k, v in list(by.items())[:6]))
    else:
        parts.append("No missed messages.")
    e = d.get("emails") or []
    ppl = [x for x in e if x["person"]]
    if e:
        parts.append(f"{len(e)} new unread email(s)" + (" — from people: " + "; ".join(
            f"{x['from']}: {x['subject']}" for x in ppl[:5]) if ppl else " (newsletters/notifications only)"))
    else:
        parts.append("No new emails.")
    n = d.get("news") or []
    if n:
        parts.append("Big news they missed (pick the 2 most important): " + "; ".join(x["title"] for x in n[:4]))
    return " ".join(parts)


def inbox_json() -> dict:
    return {"messages": list(reversed(_load(MSG_FILE, [])))[:40],
            "emails": list(reversed(_load(MAIL_FILE, [])))[:40],
            "news": _news_cache[1] or [],
            "accounts": [c["email"] for c in __import__("actions.email_agent", fromlist=["x"]).all_configs()]}


# ── tool ────────────────────────────────────────────────────────────────────
def inbox(args: dict | None = None, ui=None) -> str:
    a = str((args or {}).get("action") or "show").lower()
    if a in ("show", "open"):
        fx = getattr(ui, "fx", None)
        if fx:
            fx.open("inbox.html", "")
        return "The inbox window is open (messages, emails, news). Esc closes it."
    if a == "messages":
        m = list(reversed(_load(MSG_FILE, [])))[:10]
        return "\n".join(f"{x['time']} {x['app']} — {x['from']}: {x['text'][:200]}" for x in m) or "No messages yet."
    if a == "emails":
        m = list(reversed(_load(MAIL_FILE, [])))[:10]
        return "\n".join(f"{x['time']} [{x['account']}] {x['from']}: {x['subject']} — {x['snippet'][:120]}" for x in m) \
            or "No new emails caught yet (email_agent read_recent shows the inbox)."
    if a == "news":
        return "\n".join(f"{x['title']} ({x['source']})" for x in headlines(6)) or "No headlines right now."
    return "action must be show | messages | emails | news"


def start(brinko, ui):
    global _started, _brinko, _ui
    if _started:
        return
    _started, _brinko, _ui = True, brinko, ui
    threading.Thread(target=_message_loop, daemon=True, name="inbox-messages").start()
    threading.Thread(target=_mail_loop, daemon=True, name="inbox-mail").start()

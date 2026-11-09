"""Background WhatsApp through WhatsApp Web (Playwright, own persistent profile).

  setup()   - visible window with the QR code; the user scans it once with their phone.
              The login is kept in config/whatsapp_web/ (BRINKO's own browser profile).
  send()    - hidden browser: search contact -> open chat -> CHECK the chat header is that
              person -> type -> Enter -> wait until the message bubble shows up.
              Never guesses: if the chat can't be confirmed, nothing is sent.
  schedule  - config/scheduled_messages.json, checked every 20 s by start_scheduler().

All Playwright work happens on ONE worker thread (the sync API is thread-bound).
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROFILE = BASE_DIR / "config" / "whatsapp_web"
SCHEDULE = BASE_DIR / "config" / "scheduled_messages.json"
READY_FLAG = PROFILE / ".brink_logged_in"
URL = "https://web.whatsapp.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/146.0.0.0 Safari/537.36")
IDLE_CLOSE = 600          # close the hidden browser after 10 min without work

_jobs: "queue.Queue[tuple]" = queue.Queue()
_worker = None


def is_ready() -> bool:
    return READY_FLAG.exists()


# ── worker thread ─────────────────────────────────────────────────────────
class _Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="WhatsAppWeb")
        self.pw = self.ctx = self.page = None
        self.headed = None
        self.last_used = 0.0

    def run(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        while True:
            try:
                fn, args, box = _jobs.get(timeout=30)
            except queue.Empty:
                if self.ctx and time.time() - self.last_used > IDLE_CLOSE:
                    self._close()
                continue
            try:
                box["result"] = fn(self, *args)
            except Exception as e:
                box["result"] = f"WhatsApp Web error: {e}"
            finally:
                self.last_used = time.time()
                box["done"].set()

    def _close(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        self.ctx = self.page = None
        self.headed = None

    def browser(self, headed: bool):
        if self.ctx and self.headed == headed:
            return self.page
        self._close()
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(PROFILE), headless=not headed, user_agent=UA, viewport={"width": 1280, "height": 860},
            args=["--disable-blink-features=AutomationControlled"])
        self.headed = headed
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self.page


def _run(fn, *args, timeout: float = 240) -> str:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = _Worker()
        _worker.start()
    box = {"done": threading.Event(), "result": None}
    _jobs.put((fn, args, box))
    if not box["done"].wait(timeout):
        return "WhatsApp Web took too long."
    return box["result"]


# ── steps (run on the worker) ───────────────────────────────────────────────
_CHATLIST = "#pane-side, div[aria-label='Chat list'], div[aria-label='Chats']"


def _open(page) -> str:
    """'ok' (chat list visible), 'qr' (needs login) or 'loading'."""
    if not page.url.startswith(URL):
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    for _ in range(60):
        if page.locator(_CHATLIST).count():
            return "ok"
        if page.locator("canvas[aria-label*='QR' i], div[data-ref] canvas, [data-testid='qrcode']").count():
            return "qr"
        page.wait_for_timeout(500)
    return "loading"


def _setup(w: "_Worker") -> str:
    page = w.browser(headed=True)
    state = _open(page)
    if state == "ok":
        READY_FLAG.write_text(datetime.now().isoformat())
        w._close()
        return "WhatsApp Web is already connected. Messages can now be sent in the background."
    page.bring_to_front()
    deadline = time.time() + 180
    while time.time() < deadline:                         # wait for the scan
        if page.locator(_CHATLIST).count():
            page.wait_for_timeout(4000)                   # let it finish syncing
            READY_FLAG.write_text(datetime.now().isoformat())
            w._close()
            return "WhatsApp Web is connected. From now on messages are sent in the background."
        page.wait_for_timeout(1000)
    w._close()
    return "The QR code wasn't scanned within 3 minutes. Say 'set up WhatsApp' to try again."


def _name_score(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def _send(w: "_Worker", receiver: str, text: str) -> str:
    page = w.browser(headed=False)
    state = _open(page)
    if state == "qr":
        try:
            READY_FLAG.unlink()
        except OSError:
            pass
        return "NOT SENT - WhatsApp Web is logged out. Ask the user to say 'set up WhatsApp' and scan the QR code."
    if state != "ok":
        return "NOT SENT - WhatsApp Web didn't load."
    # search the contact
    box = page.locator("div[contenteditable='true'][data-tab='3'], div[role='textbox'][aria-label*='Search' i], "
                       "input[aria-label*='Search' i]").first
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.insert_text(receiver)
    page.wait_for_timeout(1800)
    cands = page.locator("#pane-side span[title], div[aria-label*='Search results' i] span[title]")
    best, best_s = None, 0.0
    for i in range(min(cands.count(), 12)):
        t = cands.nth(i).get_attribute("title") or ""
        sc = _name_score(receiver, t)
        if sc > best_s:
            best, best_s = cands.nth(i), sc
    if not best or best_s < 0.75:
        page.keyboard.press("Escape")
        return f"NOT SENT - no WhatsApp chat matching '{receiver}'. Ask for the exact name as saved in WhatsApp."
    best.click()
    page.wait_for_timeout(1200)
    # verify the open chat
    head = page.locator("#main header span[title], #main header span[dir='auto']").first
    open_name = (head.get_attribute("title") or head.inner_text() or "").strip() if head.count() else ""
    if _name_score(receiver, open_name) < 0.75:
        return f"NOT SENT - the open chat is '{open_name}', not '{receiver}'."
    comp = page.locator("#main footer div[contenteditable='true']").last
    comp.click()
    for i, line in enumerate(text.split("\n")):
        if i:
            page.keyboard.press("Shift+Enter")
        page.keyboard.insert_text(line)
    page.keyboard.press("Enter")
    # wait for the outgoing bubble
    probe = text.strip().split("\n")[0][:40]
    for _ in range(16):
        outs = page.locator("#main div.message-out")
        if outs.count() and probe[:20] in (outs.last.inner_text() or ""):
            return f"Sent to {open_name} on WhatsApp (in the background): \"{text}\""
        page.wait_for_timeout(500)
    return f"Probably sent to {open_name} on WhatsApp - I typed it and pressed Enter but couldn't see the bubble yet."


def _probe(w: "_Worker") -> str:
    page = w.browser(headed=False)
    return _open(page)


# ── public API ──────────────────────────────────────────────────────────────
def setup() -> str:
    return _run(_setup, timeout=240)


def send(receiver: str, text: str) -> str:
    return _run(_send, receiver, text, timeout=120)


def status() -> str:
    if not is_ready():
        return "not set up"
    st = _run(_probe, timeout=90)
    return {"ok": "connected", "qr": "logged out", "loading": "not loading"}.get(st, st)


# ── scheduled messages ──────────────────────────────────────────────────────
def _load_sched() -> list[dict]:
    try:
        return json.loads(SCHEDULE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_sched(items: list[dict]):
    SCHEDULE.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")


def schedule(receiver: str, text: str, when: datetime, platform: str = "whatsapp") -> str:
    items = _load_sched()
    items.append({"id": uuid.uuid4().hex[:8], "when": when.isoformat(timespec="minutes"), "platform": platform,
                  "receiver": receiver, "text": text, "status": "pending"})
    _save_sched(items)
    return f"Scheduled for {when.strftime('%a %H:%M')}: message to {receiver} on {platform}."


def pending() -> list[dict]:
    return [i for i in _load_sched() if i.get("status") == "pending"]


def cancel(receiver: str = "") -> str:
    items = _load_sched()
    n = 0
    for i in items:
        if i.get("status") == "pending" and (not receiver or _name_score(receiver, i["receiver"]) > 0.75):
            i["status"] = "cancelled"
            n += 1
    _save_sched(items)
    return f"Cancelled {n} scheduled message(s)."


def start_scheduler(on_result=None):
    def loop():
        while True:
            try:
                items = _load_sched()
                now = datetime.now()
                changed = False
                for it in items:
                    if it.get("status") != "pending" or datetime.fromisoformat(it["when"]) > now:
                        continue
                    it["status"] = "sending"
                    _save_sched(items)
                    from actions.send_message import _deliver
                    res = _deliver(it.get("platform", "whatsapp"), it["receiver"], it["text"])
                    it["status"] = "sent" if res.startswith("Sent") or res.startswith("Probably") else "failed"
                    it["result"] = res[:200]
                    changed = True
                    if on_result:
                        on_result(it, res)
                if changed:
                    _save_sched(items)
            except Exception as e:
                print(f"[WhatsApp] scheduler: {e}")
            time.sleep(20)
    threading.Thread(target=loop, daemon=True, name="MsgScheduler").start()

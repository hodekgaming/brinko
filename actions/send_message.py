# actions/send_message.py
# Smart texting — WhatsApp, Telegram, Instagram.
#
#   1. DRAFT: BRINKO writes the message from what the user wants to say
#      (smart_writer: right language, tone and length) and reads it back.
#   2. SEND (after the user says yes): opens the app (no Windows Search),
#      searches the contact, VERIFIES with a screenshot + Gemini vision that
#      the open chat really is that person, then pastes the message via the
#      clipboard (umlauts, emoji and line breaks survive) and sends it.
#      If the chat can't be verified, it stops — it never guesses.

from __future__ import annotations

import json
import re
import time

import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.06

_LAST_DRAFT: dict = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _platform_key(platform: str) -> str:
    p = (platform or "whatsapp").lower()
    if any(k in p for k in ("whatsapp", "wapp", "wp")):
        return "whatsapp"
    if any(k in p for k in ("telegram", "tg")):
        return "telegram"
    if any(k in p for k in ("instagram", "insta", "ig")):
        return "instagram"
    return p


def _paste(text: str):
    """Types via the clipboard (pyautogui.write can't do ä/ö/ü/ß or emoji),
    restoring whatever the user had copied before."""
    import pyperclip
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.25)
    if previous is not None:
        def _restore():
            time.sleep(1.5)
            try:
                pyperclip.copy(previous)
            except Exception:
                pass
        import threading
        threading.Thread(target=_restore, daemon=True).start()


def _focus_window(title_part: str, timeout: float = 12.0):
    """Waits for a window whose title contains `title_part`, brings it to the
    front and returns it (or None)."""
    import pygetwindow as gw
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = [w for w in gw.getAllWindows() if title_part.lower() in (w.title or "").lower()
                and w.width > 300 and w.height > 300]
        if wins:
            w = wins[0]
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
            except Exception:
                try:
                    import ctypes
                    ctypes.windll.user32.keybd_event(0, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0, 0, 2, 0)
                    ctypes.windll.user32.SetForegroundWindow(w._hWnd)
                except Exception:
                    pass
            time.sleep(0.6)
            return w
        time.sleep(0.4)
    return None


def _verify_chat(window, receiver: str) -> tuple[bool, str]:
    """Screenshot of the app window → Gemini vision: whose chat is open?"""
    try:
        import base64, io
        from or_client import client
        img = pyautogui.screenshot(region=(max(0, window.left), max(0, window.top),
                                           window.width, window.height))
        img.thumbnail((1400, 1400))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        ans = client.vision(
            f"This is a messaging app. Which conversation is currently OPEN on the right side "
            f"(the name in the chat header at the top of the message area)? Does it match the "
            f"contact '{receiver}' (same person; ignore emoji/case/extra surname)? "
            'Reply ONLY with JSON: {"open_chat": "<name in header or none>", "matches": true|false}',
            base64.b64encode(buf.getvalue()).decode(),
            system="You read app screenshots precisely. JSON only.",
        )
        m = re.search(r"\{.*\}", ans, re.S)
        data = json.loads(m.group(0)) if m else {}
        return bool(data.get("matches")), str(data.get("open_chat") or "?")
    except Exception as e:
        print(f"[SendMessage] ⚠️ verify failed: {e}")
        return False, "?"


def _open_chat_desktop(app: str, receiver: str):
    """Opens the desktop app in FRONT (the user needs to see what's sent),
    searches the contact and opens the chat. Returns the window or None."""
    from actions.open_app import open_app
    open_app({"app_name": app, "background": False})
    title = {"whatsapp": "WhatsApp", "telegram": "Telegram"}.get(app.lower(), app)
    win = _focus_window(title)
    if not win:
        return None
    pyautogui.press("esc")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "a")
    _paste(receiver)
    time.sleep(1.4)
    pyautogui.press("enter")
    time.sleep(1.2)
    return win


def _deliver(platform: str, receiver: str, text: str) -> str:
    key = _platform_key(platform)

    if key == "instagram":
        from actions.browser_util import open_url
        open_url("https://www.instagram.com/direct/new/")
        time.sleep(4)
        _paste(receiver)
        time.sleep(1.6)
        pyautogui.press("down"); time.sleep(0.2); pyautogui.press("enter"); time.sleep(0.4)
        for _ in range(3):
            pyautogui.press("tab"); time.sleep(0.1)
        pyautogui.press("enter")
        time.sleep(2)
        _paste(text)
        pyautogui.press("enter")
        return f"Sent to {receiver} on Instagram."

    if key == "whatsapp":
        # hidden WhatsApp Web once it's set up: never takes over the screen
        try:
            from actions import whatsapp_web
            if whatsapp_web.is_ready():
                res = whatsapp_web.send(receiver, text)
                if "logged out" not in res and "didn't load" not in res:
                    return res
                print(f"[SendMessage] web: {res} - using the desktop app")
        except Exception as e:
            print(f"[SendMessage] web send failed: {e}")

    app = {"whatsapp": "WhatsApp", "telegram": "Telegram"}.get(key, platform)
    win = _open_chat_desktop(app, receiver)
    if not win:
        return f"Couldn't open {app}. Is it installed and logged in?"

    ok, open_chat = _verify_chat(win, receiver)
    if not ok:
        pyautogui.press("esc")
        return (f"NOT SENT — I opened {app} but couldn't confirm the chat is {receiver} "
                f"(the open chat looks like '{open_chat}'). Tell the user, and ask for the exact "
                f"contact name as saved in {app}.")

    _paste(text)
    time.sleep(0.3)
    pyautogui.press("enter")
    return f"Sent to {open_chat} on {app}: \"{text}\""


# ── tool entry point ─────────────────────────────────────────────────────────
def send_message(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """
    parameters:
        receiver     : contact name as saved in the app
        intent       : what the user wants to say, in their words → BRINKO writes the text
        message_text : exact text to send (skips writing)
        platform     : whatsapp (default) | telegram | instagram
        action       : draft (write + read back, default) | send
        tone, language : optional
    """
    p        = parameters or {}
    action   = str(p.get("action") or "draft").lower()
    receiver = str(p.get("receiver") or _LAST_DRAFT.get("receiver") or "").strip()
    platform = str(p.get("platform") or _LAST_DRAFT.get("platform") or "whatsapp").strip()
    text     = str(p.get("message_text") or "").strip()
    intent   = str(p.get("intent") or "").strip()

    if not receiver:
        return "Who should I message?"

    if action == "send" and not text and not intent and _LAST_DRAFT.get("text") \
            and _LAST_DRAFT.get("receiver", "").lower() == receiver.lower():
        text = _LAST_DRAFT["text"]            # "yes, send it"

    if not text:
        if not intent:
            return "What should the message say?"
        from actions.smart_writer import compose_text
        out  = compose_text(intent, recipient=receiver, platform=platform.title(),
                            tone=str(p.get("tone") or ""), language=str(p.get("language") or ""))
        text = str(out.get("text") or "").strip()
        if not text:
            return "I couldn't write that message — try saying it differently."

    _LAST_DRAFT.update({"receiver": receiver, "platform": platform, "text": text})

    if action != "send":
        return (f"DRAFT to {receiver} ({platform}): \"{text}\" — read it to the user exactly and ask "
                f"if you should send it. If yes, call send_message action=send receiver='{receiver}' "
                f"(no text needed — the draft is remembered).")

    when = str(p.get("when") or "").strip()
    if when:
        from datetime import datetime
        try:
            at = datetime.fromisoformat(when.replace("Z", ""))
        except ValueError:
            return f"I couldn't read the time '{when}' - use local ISO time like 2026-11-08T18:30."
        if at <= datetime.now():
            return "That time has already passed - when should it go out?"
        from actions import whatsapp_web
        _LAST_DRAFT.clear()
        return whatsapp_web.schedule(receiver, text, at, platform)

    print(f"[SendMessage] 📨 {platform} → {receiver}: {text[:60]}")
    if player:
        player.write_log(f"[msg] Sending to {receiver} via {platform}...")
    try:
        result = _deliver(platform, receiver, text)
    except pyautogui.FailSafeException:
        result = "Stopped — the mouse was moved to a screen corner (safety stop)."
    except Exception as e:
        result = f"{platform} error: {e}"
    if result.startswith("Sent"):
        _LAST_DRAFT.clear()
    print(f"[SendMessage] {result}")
    if player:
        player.write_log(f"[msg] {result}")
    return result

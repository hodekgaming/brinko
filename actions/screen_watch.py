"""Always-on screen awareness.

A background thread keeps:
  * the foreground window (title + app) and how long it has been in front,
  * a small timeline of which apps were used (feeds memory, briefing and the
    proactive "you've been gaming for 3 hours" nudges),
  * a fresh JPEG of the monitor the user is working on, re-taken only when the
    picture actually changed.

`look_at_screen(question)` answers from that snapshot with Gemini vision, so
"what am I looking at?" / "what does this error mean?" is answered in ~1-2 s
instead of spinning up a separate session. Nothing is stored on disk except
the app timeline (app names + window titles, no pictures).
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TIMELINE_DIR = BASE_DIR / "memory" / "activity"

INTERVAL = 3.0          # seconds between checks
MAX_W = 1280            # snapshot width sent to the vision model (1024 blurs small error text)

_state = {
    "title": "", "app": "", "since": time.time(), "jpeg": b"", "shot_at": 0.0,
    "thumb": None, "monitor": None,
}
_lock = threading.Lock()
_started = False
_usage: dict[str, float] = {}        # app -> seconds today (in memory)
_usage_day = datetime.now().strftime("%Y-%m-%d")

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def _foreground() -> tuple[str, str, object]:
    """(window title, app exe name, hwnd)"""
    if not user32:
        return "", "", None
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = ""
    try:
        import psutil
        app = psutil.Process(pid.value).name().rsplit(".", 1)[0]
    except Exception:
        pass
    return buf.value, app, hwnd


def _monitor_rect(hwnd) -> dict | None:
    """The monitor the foreground window is on (multi-monitor setups)."""
    try:
        MONITOR_DEFAULTTONEAREST = 2

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        return {"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top}
    except Exception:
        return None


def _grab(sct, mon: dict) -> tuple[bytes, object]:
    from PIL import Image
    raw = sct.grab(mon)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    thumb = img.resize((48, 27)).convert("L")
    if img.width > MAX_W:
        img = img.resize((MAX_W, int(img.height * MAX_W / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=72)
    return buf.getvalue(), thumb


def _changed(a, b) -> bool:
    if a is None or b is None:
        return True
    from PIL import ImageChops
    diff = ImageChops.difference(a, b).getdata()
    return sum(diff) / len(diff) > 2.5


def _log_usage(app: str, title: str, secs: float):
    """Per-app seconds for today, flushed to memory/activity/<day>.json."""
    global _usage_day
    day = datetime.now().strftime("%Y-%m-%d")
    if day != _usage_day:
        _usage.clear()
        _usage_day = day
    if not app:
        return
    _usage[app] = _usage.get(app, 0.0) + secs


def _flush_usage():
    try:
        TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
        f = TIMELINE_DIR / f"{_usage_day}.json"
        f.write_text(json.dumps({k: int(v) for k, v in sorted(_usage.items(), key=lambda x: -x[1])}, indent=1),
                     encoding="utf-8")
    except Exception as e:
        print(f"[Screen] usage flush: {e}")


def _loop():
    import mss
    last_flush = time.time()
    last = time.time()
    with mss.mss() as sct:
        while True:
            try:
                title, app, hwnd = _foreground()
                now = time.time()
                with _lock:
                    if app != _state["app"] or title != _state["title"]:
                        if app != _state["app"]:
                            _state["since"] = now
                        _state["title"], _state["app"] = title, app
                _log_usage(app, title, now - last)
                last = now
                mon = _monitor_rect(hwnd) if hwnd else None
                if mon:
                    jpeg, thumb = _grab(sct, mon)
                    with _lock:
                        if _changed(_state["thumb"], thumb) or now - _state["shot_at"] > 30:
                            _state.update(jpeg=jpeg, thumb=thumb, shot_at=now, monitor=mon)
                if now - last_flush > 60:
                    _flush_usage()
                    last_flush = now
            except Exception as e:
                print(f"[Screen] watch: {e}")
            time.sleep(INTERVAL)


def start():
    global _started
    if _started:
        return
    _started = True
    _usage.update({k: float(v) for k, v in usage_for(_usage_day).items()})   # keep today's totals across restarts
    threading.Thread(target=_loop, daemon=True, name="ScreenWatch").start()


def context() -> dict:
    """What the user is doing right now (no picture)."""
    with _lock:
        return {"app": _state["app"], "title": _state["title"],
                "minutes_in_app": int((time.time() - _state["since"]) / 60)}


def context_line() -> str:
    c = context()
    if not c["app"]:
        return ""
    return f"The user is in {c['app']} ('{c['title'][:90]}') for {c['minutes_in_app']} min."


def usage_today() -> dict:
    return {k: int(v) for k, v in sorted(_usage.items(), key=lambda x: -x[1])}


def usage_for(day: str) -> dict:
    """App -> seconds for a past day (YYYY-MM-DD)."""
    try:
        return json.loads((TIMELINE_DIR / f"{day}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def snapshot(max_age: float = 1.5) -> bytes:
    """A current JPEG of the active monitor (re-taken if the cached one is older than max_age)."""
    with _lock:
        fresh = time.time() - _state["shot_at"] <= max_age and _state["jpeg"]
        if fresh:
            return _state["jpeg"]
    import mss
    _, _, hwnd = _foreground()
    mon = _monitor_rect(hwnd) if hwnd else None
    with mss.mss() as sct:
        jpeg, thumb = _grab(sct, mon or sct.monitors[1])
    with _lock:
        _state.update(jpeg=jpeg, thumb=thumb, shot_at=time.time())
    return jpeg


_genai = None


def _fast_vision(jpeg: bytes, prompt: str) -> str:
    """~1 s: flash-lite, no thinking, short timeout. Returns '' if it's busy."""
    global _genai
    try:
        from google import genai
        from google.genai import types
        if _genai is None:
            key = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))["gemini_api_key"]
            # no SDK retries (they back off for many seconds on a 503) - we switch model instead
            _genai = genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=12000, retry_options=types.HttpRetryOptions(attempts=1)))
        for model in ("gemini-3.1-flash-lite", "gemini-flash-lite-latest"):
            try:
                r = _genai.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), prompt],
                    config=types.GenerateContentConfig(
                        max_output_tokens=500, temperature=0.2,
                        system_instruction="You see the user's screen. Be precise and brief. Never invent text you can't read.",
                        **({"thinking_config": types.ThinkingConfig(thinking_budget=0)} if "3.1" in model else {})))
                if r.text:
                    return r.text
            except Exception as e:
                print(f"[Screen] {model}: {str(e)[:80]}")
    except Exception as e:
        print(f"[Screen] fast vision: {e}")
    return ""


def look_at_screen(parameters: dict, player=None) -> str:
    """Tool: answer a question about what's on the user's screen right now."""
    q = str((parameters or {}).get("question") or "What is on the screen and what is the user doing?").strip()
    try:
        jpeg = snapshot()
    except Exception as e:
        return f"Couldn't capture the screen: {e}"
    ctx = context_line()
    prompt = (f"{ctx}\nThe user asks about their screen: \"{q}\"\n"
              "Answer from the screenshot like a sharp assistant looking over their shoulder: concrete, "
              "specific (quote exact error text, names, numbers you can read), max 4 short sentences. "
              "If they ask how to fix something, give the actual fix. If something isn't readable, say so.")
    # race: fast model first; if it hasn't answered in 3.5 s, the general client joins in
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    def _general():
        from or_client import client
        return client.vision(prompt, base64.b64encode(jpeg).decode(), mime="image/jpeg",
                             system="You see the user's screen. Be precise and brief. Never invent text you can't read.",
                             max_tokens=900)
    ex = ThreadPoolExecutor(max_workers=2)
    futs = [ex.submit(_fast_vision, jpeg, prompt)]
    done, _ = wait(futs, timeout=3.5)
    ans = futs[0].result() if done else ""
    if not ans:
        if not done:
            futs.append(ex.submit(_general))
        else:
            futs = [ex.submit(_general)]
        deadline = time.time() + 25
        while futs and time.time() < deadline and not ans:
            done, _ = wait(futs, timeout=deadline - time.time(), return_when=FIRST_COMPLETED)
            for f in done:
                futs.remove(f)
                try:
                    ans = f.result() or ans
                except Exception as e:
                    print(f"[Screen] vision: {e}")
    ex.shutdown(wait=False)
    return (ans or "I couldn't make out the screen.").strip()

# phone_brain.py
#
# The brain behind the BRINKO phone app.
#
# The PC's voice session is audio-only and speaks out of the PC speakers —
# useless when you're out. So phone requests get their own text agent
# (Gemini with function calling) that can use:
#
#   • PC tools  — the same ones BRINKO uses at home (open apps on the PC,
#                 email, files, web search, weather, deep thinking, coding
#                 jobs, reminders…). Slow ones become background jobs and
#                 the phone is notified when they finish.
#   • PHONE tools — things that must happen ON the phone: call, text
#                 (SMS/WhatsApp), email, navigate, play music, open apps,
#                 timers. These aren't executed here — they're returned as
#                 "actions" and the phone app carries them out (Android
#                 links/intents), so the user always sees and confirms.

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from urllib.parse import quote

# PC tools that make sense from a phone (no mouse/keyboard puppeteering of an
# empty room, no PC-speaker-only tools).
PC_TOOLS_FOR_PHONE = {
    "open_app", "open_file", "web_search", "weather_report", "music", "email_agent",
    "deep_think", "computer_settings", "file_controller", "code_helper", "dev_agent",
    "background_jobs", "reminder", "schedule_task", "night_mode", "game_updater",
    "youtube_video", "flight_finder", "ad_content", "agent_task", "desktop_control",
    "save_memory",
}

PHONE_TOOLS = [
    {
        "name": "phone_call",
        "description": "Calls someone FROM THE PHONE (opens the dialer with the number ready). "
                       "Use for 'call mom', 'ring Jake'. Give the contact name as the user says it.",
        "parameters": {"type": "OBJECT", "properties": {
            "contact": {"type": "STRING", "description": "Who to call, e.g. 'Mom'"},
            "number":  {"type": "STRING", "description": "Only if the user said a number"},
        }, "required": ["contact"]},
    },
    {
        "name": "phone_text",
        "description": "Texts someone FROM THE PHONE via WhatsApp (default) or SMS. BRINKO writes the "
                       "message from what the user wants to say; the phone opens the chat with it "
                       "pre-filled and the user taps send. Pass the user's intent, not a finished text.",
        "parameters": {"type": "OBJECT", "properties": {
            "contact": {"type": "STRING", "description": "Who, e.g. 'Mom', 'Lukas'"},
            "intent":  {"type": "STRING", "description": "What they want to say, in their words"},
            "exact_text": {"type": "STRING", "description": "Only if they dictated the exact words"},
            "app":     {"type": "STRING", "description": "whatsapp (default) | sms | telegram"},
            "number":  {"type": "STRING", "description": "Only if the user said a number"},
        }, "required": ["contact"]},
    },
    {
        "name": "phone_email",
        "description": "Writes an email and opens it ready-to-send in the PHONE's mail app (Gmail). "
                       "Use when the user is on the phone and wants to email someone.",
        "parameters": {"type": "OBJECT", "properties": {
            "to":      {"type": "STRING", "description": "Recipient address or name"},
            "request": {"type": "STRING", "description": "What the email should say, in their words"},
        }, "required": ["request"]},
    },
    {
        "name": "phone_navigate",
        "description": "Starts navigation / shows a place in Google Maps on the phone.",
        "parameters": {"type": "OBJECT", "properties": {
            "destination": {"type": "STRING", "description": "Address or place, e.g. 'nearest McDonald's', 'Marienplatz Munich'"},
            "mode": {"type": "STRING", "description": "driving (default) | walking | transit | bicycling"},
        }, "required": ["destination"]},
    },
    {
        "name": "phone_play",
        "description": "Plays music/video ON THE PHONE (Spotify by default, or YouTube).",
        "parameters": {"type": "OBJECT", "properties": {
            "query": {"type": "STRING", "description": "Song, artist, playlist or video"},
            "app":   {"type": "STRING", "description": "spotify (default) | youtube | youtube_music"},
        }, "required": ["query"]},
    },
    {
        "name": "phone_open_app",
        "description": "Opens an app ON THE PHONE (Android).",
        "parameters": {"type": "OBJECT", "properties": {
            "app_name": {"type": "STRING", "description": "e.g. 'Instagram', 'Bambu Handy'"},
            "android_package": {"type": "STRING", "description": "The app's Android package id if you know it, e.g. 'com.instagram.android'"},
        }, "required": ["app_name"]},
    },
    {
        "name": "phone_open_url",
        "description": "Opens a website or search on the phone's browser.",
        "parameters": {"type": "OBJECT", "properties": {
            "url":   {"type": "STRING", "description": "Full URL"},
            "query": {"type": "STRING", "description": "Or a Google search query"},
        }},
    },
    {
        "name": "phone_timer",
        "description": "Starts a countdown timer in the BRINKO phone app (it rings and notifies when done).",
        "parameters": {"type": "OBJECT", "properties": {
            "seconds": {"type": "INTEGER", "description": "Total seconds"},
            "label":   {"type": "STRING", "description": "e.g. 'pizza'"},
        }, "required": ["seconds"]},
    },
    {
        "name": "phone_alarm",
        "description": "Alarm for a clock time. Opens the phone's Clock app so the user can confirm it "
                       "(web apps can't silently set system alarms on Android).",
        "parameters": {"type": "OBJECT", "properties": {
            "time":  {"type": "STRING", "description": "HH:MM 24h"},
            "label": {"type": "STRING", "description": "Optional label"},
        }, "required": ["time"]},
    },
]

# Well-known Android apps: deep-link scheme (opens the app) + package.
KNOWN_APPS = {
    "spotify":   ("spotify://", "com.spotify.music"),
    "whatsapp":  ("whatsapp://", "com.whatsapp"),
    "instagram": ("instagram://app", "com.instagram.android"),
    "youtube":   ("vnd.youtube://", "com.google.android.youtube"),
    "youtube music": ("", "com.google.android.apps.youtube.music"),
    "telegram":  ("tg://", "org.telegram.messenger"),
    "discord":   ("discord://", "com.discord"),
    "tiktok":    ("snssdk1233://", "com.zhiliaoapp.musically"),
    "snapchat":  ("snapchat://", "com.snapchat.android"),
    "netflix":   ("nflx://", "com.netflix.mediaclient"),
    "twitch":    ("twitch://", "tv.twitch.android.app"),
    "gmail":     ("googlegmail://", "com.google.android.gm"),
    "maps":      ("geo:0,0", "com.google.android.apps.maps"),
    "google maps": ("geo:0,0", "com.google.android.apps.maps"),
    "chrome":    ("googlechrome://", "com.android.chrome"),
    "camera":    ("", "com.android.camera"),
    "clock":     ("", "com.google.android.deskclock"),
    "calendar":  ("", "com.google.android.calendar"),
    "photos":    ("", "com.google.android.apps.photos"),
    "chatgpt":   ("", "com.openai.chatgpt"),
    "reddit":    ("reddit://", "com.reddit.frontpage"),
    "steam":     ("", "com.valvesoftware.android.steam.community"),
    "bambu handy": ("", "bbl.intl.bambulab.com"),
    "x":         ("twitter://", "com.twitter.android"),
    "twitter":   ("twitter://", "com.twitter.android"),
}


def _intent_url(package: str, scheme: str = "", fallback: str = "") -> str:
    fb = fallback or f"https://play.google.com/store/apps/details?id={package}"
    if scheme:
        return scheme
    return f"intent://#Intent;package={package};action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;S.browser_fallback_url={quote(fb, safe='')};end"


def brinko_core_prompt() -> str:
    """BRINKO's real personality + 'being genuinely smart' rules — the SAME text
    the PC voice uses (core/prompt.txt, everything before the PC-specific
    RULES section), so the phone is the same BRINKO, not a different bot."""
    try:
        from pathlib import Path
        txt = (Path(__file__).resolve().parent / "core" / "prompt.txt").read_text(encoding="utf-8")
        core = txt.split("\nRULES:")[0]
        return core.split("\nWRITING EMAILS & TEXTS")[0].strip()
    except Exception:
        return "You are BRINKO — a sharp, capable AI assistant with a dry sense of humor."


PHONE_CONTEXT = """[RIGHT NOW] The user is talking to you through the BRINKO app on their ANDROID PHONE
(they may be out; their PC is at home). Same you, same memory — just a different device.

Where things happen:
- Phone-side requests → phone_* tools: calling, texting (phone_text — NOT send_message), emailing
  from the phone, navigation, music on the phone, opening phone apps, timers/alarms.
  "open spotify" said on the phone = phone_open_app.
- Requests that mention the PC/computer/laptop, or only make sense there (a PC program, a file on
  the PC, downloads/updates, coding, lock the PC, PC volume) → the PC tools.
- Facts/current info → web_search (live results). Real reasoning (maths, plans, comparisons,
  advice) → deep_think.
- Texts/emails: pass the user's intent; BRINKO's writer produces the words. The phone opens the app
  pre-filled and the user taps send — no need to ask "send it?" first.

Replies appear in a chat AND are spoken in your voice: 1–3 short sentences, plain text, no
markdown. Reply in the user's language. Only report what tools actually returned."""


class PhoneBrain:
    MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash"]

    def __init__(self):
        self.brinko = None
        self.pc_tools: list[dict] = []
        self.job_summary = None
        self._client = None
        self._events: list[dict] = []
        self._event_id = 0
        self._lock = threading.Lock()

    # ── wiring ───────────────────────────────────────────────────────────
    def attach(self, brinko, tool_declarations: list[dict], job_summary=None):
        self.brinko = brinko
        self.pc_tools = [t for t in tool_declarations if t.get("name") in PC_TOOLS_FOR_PHONE]
        self.job_summary = job_summary

    def push_event(self, kind: str, text: str, **extra):
        with self._lock:
            self._event_id += 1
            self._events.append({"id": self._event_id, "type": kind, "text": text, "t": time.time(), **extra})
            self._events = self._events[-200:]

    def events_since(self, since: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["id"] > since]

    def _gemini(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            key = json.loads((__import__("pathlib").Path(__file__).resolve().parent / "config" / "api_keys.json")
                             .read_text(encoding="utf-8"))["gemini_api_key"]
            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=12000, retry_options=types.HttpRetryOptions(attempts=1)))   # stuck model → next one fast
        return self._client

    # ── main entry ───────────────────────────────────────────────────────
    def ask(self, text: str, history: list[dict] | None = None) -> dict:
        from google.genai import types

        actions: list[dict] = []
        used: list[str] = []
        contents = []
        for h in (history or [])[-12:]:
            role = "model" if h.get("role") in ("brinko", "model", "assistant") else "user"
            if h.get("text"):
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=str(h["text"])[:1500])]))
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))

        system = self._system_prompt()
        tools = [types.Tool(function_declarations=PHONE_TOOLS + self.pc_tools)]
        cfg = types.GenerateContentConfig(
            system_instruction=system, tools=tools, temperature=0.5, max_output_tokens=1200,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        reply = ""
        for _ in range(6):
            resp = self._generate(contents, cfg)
            if resp is None:
                return {"reply": "My brain's having connection trouble right now — try again in a moment.",
                        "actions": actions, "tools": used}
            calls = resp.function_calls or []
            if not calls:
                reply = (resp.text or "").strip()
                break
            contents.append(resp.candidates[0].content)
            parts = []
            for call in calls:
                args = dict(call.args or {})
                used.append(call.name)
                try:
                    result = self._run_tool(call.name, args, actions)
                except Exception as e:
                    result = f"Tool failed: {e}"
                parts.append(types.Part.from_function_response(name=call.name, response={"result": str(result)[:4000]}))
            contents.append(types.Content(role="user", parts=parts))
        if not reply:
            reply = "Done." if actions or used else "Hmm, I didn't catch that — say it another way?"
        if self.brinko is not None:
            try:
                self.brinko.ui.write_log(f"SYS: 📱 Phone: {text[:60]}")
                # one shared conversation: the PC voice sees what was said on the phone
                self.brinko._recent.append(f"User (on phone): {text[:300]}")
                self.brinko._recent.append(f"BRINKO (on phone): {reply[:300]}")
                from memory import journal
                journal.log_exchange(text, reply, "phone")
            except Exception:
                pass
        return {"reply": reply, "actions": actions, "tools": used}

    def _generate(self, contents, cfg):
        last = None
        for model in self.MODELS:
            try:
                return self._gemini().models.generate_content(model=model, contents=contents, config=cfg)
            except Exception as e:
                last = e
                continue
        print(f"[PhoneBrain] ⚠️ all models failed: {last}")
        return None

    def _system_prompt(self) -> str:
        mem = ""
        try:
            from memory.memory_manager import load_memory, format_memory_for_prompt
            mem = format_memory_for_prompt(load_memory())
        except Exception:
            pass
        now = datetime.now().strftime("%A %d %B %Y, %H:%M")
        pc = "The PC is ON and BRINKO is running there." if self.brinko else "BRINKO's PC tools aren't ready yet."
        recent = ""
        if self.brinko is not None and getattr(self.brinko, "_recent", None):
            recent = ("[RECENT CONVERSATION — PC and phone together, oldest first]\n"
                      + "\n".join(list(self.brinko._recent)[-12:]))
        return f"{brinko_core_prompt()}\n\n{PHONE_CONTEXT}\n\n[NOW] {now}\n[PC] {pc}\n{mem}\n{recent}"

    # ── tools ────────────────────────────────────────────────────────────
    def _run_tool(self, name: str, args: dict, actions: list[dict]) -> str:
        if name.startswith("phone_"):
            return self._phone_tool(name, args, actions)
        if name == "save_memory":
            from memory.memory_manager import update_memory
            if args.get("key") and args.get("value"):
                update_memory({args.get("category", "notes"): {args["key"]: {"value": args["value"]}}})
            return "saved"
        if self.brinko is None:
            return "The PC part of BRINKO isn't running yet."

        b = self.brinko
        try:
            from agent.personas import persona_for
            b.ui.flash_agent(persona_for(name))
        except Exception:
            pass

        def phone_say(text: str):
            self.push_event("say", str(text))

        if b._should_background(name, args):
            from agent.background_jobs import get_jobs

            def done(job):
                ok = job.status == "done"
                self.push_event("job", f"{'✓' if ok else '✗'} {job.tool} {'finished' if ok else 'failed'}: "
                                       f"{job.result[:400]}", job_id=job.job_id, ok=ok)
                try:
                    b.ui.write_log(f"SYS: 📱 Phone job #{job.job_id} {job.tool} {job.status}")
                except Exception:
                    pass

            summary = self.job_summary(name, args) if self.job_summary else name
            job = get_jobs().submit(name, summary, lambda: b._dispatch_sync(name, args, speak=phone_say),
                                    on_done=done, origin="phone")
            return (f"Started on the PC as background job #{job.job_id}. The phone will get a "
                    f"notification when it's done — tell the user that.")
        return b._dispatch_sync(name, args, speak=phone_say)

    def _phone_tool(self, name: str, a: dict, actions: list[dict]) -> str:
        if name == "phone_call":
            actions.append({"type": "call", "contact": a.get("contact", ""), "number": a.get("number", "")})
            return f"The phone is calling {a.get('contact')} (it may ask the user to pick the contact once)."

        if name == "phone_text":
            app = (a.get("app") or "whatsapp").lower()
            text = (a.get("exact_text") or "").strip()
            if not text:
                from actions.smart_writer import compose_text
                out = compose_text(a.get("intent") or "", recipient=a.get("contact", ""),
                                   platform="SMS" if app == "sms" else app.title())
                text = str(out.get("text") or "").strip()
            actions.append({"type": "text", "contact": a.get("contact", ""), "number": a.get("number", ""),
                            "app": app, "text": text})
            return f"Prepared on the phone ({app}) for {a.get('contact')}: \"{text}\" — the user taps send."

        if name == "phone_email":
            from actions.smart_writer import compose_email
            mail = compose_email(a.get("request") or "", to=a.get("to", ""))
            to = a.get("to", "") if "@" in a.get("to", "") else ""
            actions.append({"type": "email", "to": to, "subject": mail.get("subject", ""), "body": mail.get("body", "")})
            return f"Email ready in the phone's mail app. Subject: {mail.get('subject', '')}"

        if name == "phone_navigate":
            mode = (a.get("mode") or "driving").lower()
            dest = a.get("destination", "")
            url = f"https://www.google.com/maps/dir/?api=1&destination={quote(dest)}&travelmode={quote(mode)}&dir_action=navigate"
            actions.append({"type": "open", "url": url, "label": f"Navigate to {dest}"})
            return f"Opening Google Maps navigation to {dest}."

        if name == "phone_play":
            q = a.get("query", "")
            app = (a.get("app") or "spotify").lower()
            if "youtube" in app and "music" in app:
                url = f"https://music.youtube.com/search?q={quote(q)}"
            elif "youtube" in app:
                url = f"https://www.youtube.com/results?search_query={quote(q)}"
            else:
                url = f"spotify:search:{quote(q)}"
            actions.append({"type": "open", "url": url, "fallback": f"https://open.spotify.com/search/{quote(q)}",
                            "label": f"Play {q}"})
            return f"Opening {q} in {app} on the phone."

        if name == "phone_open_app":
            nm = (a.get("app_name") or "").strip()
            scheme, pkg = KNOWN_APPS.get(nm.lower(), ("", a.get("android_package") or ""))
            if not pkg and not scheme:
                actions.append({"type": "open", "url": f"https://play.google.com/store/search?q={quote(nm)}&c=apps",
                                "label": f"Find {nm}"})
                return f"Don't know {nm}'s Android id — opened the Play Store search for it."
            actions.append({"type": "open", "url": _intent_url(pkg, scheme), "label": f"Open {nm}"})
            return f"Opening {nm} on the phone."

        if name == "phone_open_url":
            url = a.get("url") or f"https://www.google.com/search?q={quote(a.get('query', ''))}"
            actions.append({"type": "open", "url": url, "label": "Open link"})
            return "Opened on the phone."

        if name == "phone_timer":
            secs = int(a.get("seconds") or 0)
            actions.append({"type": "timer", "seconds": secs, "label": a.get("label", "")})
            return f"Timer set for {secs // 60} min {secs % 60} s in the BRINKO app."

        if name == "phone_alarm":
            actions.append({"type": "open", "url": _intent_url("com.google.android.deskclock"),
                            "label": f"Set alarm {a.get('time')}"})
            return (f"Opened the Clock app — Android doesn't let web apps set alarms silently, "
                    f"so the user confirms {a.get('time')} there.")
        return "Unknown phone action."


BRAIN = PhoneBrain()

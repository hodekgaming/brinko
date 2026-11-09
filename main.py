import asyncio
import threading
import json
import os
import sys
import time
import random
import platform
import traceback
from pathlib import Path

# Must happen before PyQt6/pyautogui touch the display in any way — Windows
# only lets a process set its DPI-awareness mode once. If Qt's own attempt
# loses that race (which it was: "SetProcessDpiAwarenessContext() failed:
# Zugriff verweigert" in the logs), screenshots and click coordinates end
# up in mismatched coordinate spaces on any scaled display, and clicks
# silently land in the wrong place — which looks exactly like "it can't
# click on anything."
if platform.system() == "Windows":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Launched from the desktop shortcut (pythonw.exe) there is no console at
# all — send everything to logs/brinko.log so problems are still diagnosable.
if sys.stdout is None or sys.stderr is None:
    try:
        _log_dir = Path(__file__).resolve().parent / "logs"
        _log_dir.mkdir(exist_ok=True)
        _log_path = _log_dir / "brinko.log"
        if _log_path.exists() and _log_path.stat().st_size > 2_000_000:
            _log_path.replace(_log_dir / "brinko.old.log")
        _log_file = open(_log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stdout or _log_file
        sys.stderr = sys.stderr or _log_file
    except Exception:
        pass

_MUTEX_NAME  = r"Local\BRINKO_SINGLE_INSTANCE"
_WINDOW_NAME = "BRINKO"


def _code_mtime() -> float:
    """Newest modification time of BRINKO's own code/prompt files."""
    newest = 0.0
    for pattern in ("*.py", "actions/*.py", "agent/*.py", "memory/*.py", "core/*.txt"):
        for f in BASE_DIR.glob(pattern):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    return newest


def _ensure_single_instance():
    """Only one BRINKO at a time. Launching again (desktop icon):
      • running copy is up to date → bring its window to the front, exit
      • code changed since it started → close the old copy, start fresh
        (otherwise updates never load: the icon kept refocusing a stale BRINKO)"""
    global _INSTANCE_MUTEX
    if platform.system() != "Windows":
        return
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt
        k32, u32 = _ct.windll.kernel32, _ct.windll.user32
        _INSTANCE_MUTEX = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if k32.GetLastError() != 183:   # ERROR_ALREADY_EXISTS
            return
        hwnd = u32.FindWindowW(None, _WINDOW_NAME)
        old_pid = None
        if hwnd:
            pid = _wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, _ct.byref(pid))
            old_pid = pid.value or None

        stale = False
        if old_pid:
            try:
                import psutil
                stale = _code_mtime() > psutil.Process(old_pid).create_time()
            except Exception:
                stale = False

        if not stale:
            if hwnd:
                u32.ShowWindow(hwnd, 9)          # SW_RESTORE
                u32.SetForegroundWindow(hwnd)
            sys.exit(0)

        print("[BRINKO] ♻️ Code was updated — restarting the running BRINKO with the new version.")
        u32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE → clean shutdown (restores volumes)
        try:
            import psutil
            p = psutil.Process(old_pid)
            try:
                p.wait(timeout=8)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
        except Exception:
            pass
        k32.CloseHandle(_INSTANCE_MUTEX)
        _INSTANCE_MUTEX = k32.CreateMutexW(None, False, _MUTEX_NAME)
    except SystemExit:
        raise
    except Exception:
        pass


# Emoji-heavy log lines must never crash a tool just because the console
# isn't UTF-8 (e.g. output redirected to a file, or a cp1252 terminal).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import audioop
    _HAS_AUDIOOP = True
except ImportError:
    audioop = None
    _HAS_AUDIOOP = False

from google import genai
from google.genai import types

try:
    import google.genai as _gv
    print(f"[BRINKO] google-genai SDK version: {getattr(_gv, '__version__', 'unknown')}")
except Exception:
    pass

try:
    import sounddevice as sd
except ImportError:
    sd = None

from ui import BrinkoUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    should_extract_memory, extract_memory
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.music             import music
from actions.email_agent       import email_agent
from actions.phone_call        import phone_call
from actions.reminder          import reminder
from actions.schedule_task     import schedule_task
from actions.night_mode        import night_mode, read_morning_report
from actions.computer_settings import computer_settings
from actions.screen_processor  import screen_process
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.cmd_control       import cmd_control
from actions.stream_control    import stream_control
from actions.ad_content        import ad_content
from actions.ultra_mode        import ultra_mode, is_ultra_enabled, ULTRA_SYSTEM_BLOCK
from actions.weather_report    import get_weather_summary, format_weather_spoken
from agent.personas            import persona_prefix, persona_for, roll_call_text, AGENT_RING
from agent.background_jobs     import get_jobs

# Tools that take a while. These run as background jobs: the tool call
# returns instantly, BRINKO keeps talking/listening, and the result is
# announced when the job finishes — so you can keep doing your own thing.
BACKGROUND_TOOLS = {
    "create_document", "agent_task", "code_project",
    "dev_agent", "code_helper", "file_processor", "flight_finder",
    "ad_content", "game_updater", "email_agent", "youtube_video",
}
# Quick sub-actions of background tools that are better answered inline.
_INLINE_ACTIONS = {
    "code_helper":  {"explain"},
    "game_updater": {"list", "download_status", "schedule_status"},
    "youtube_video": {"play", "trending", "get_info", ""},
    "email_agent":  {"read_recent"},
}


def _job_summary(name: str, args: dict) -> str:
    for k in ("description", "goal", "topic", "query", "instruction", "game_name",
              "destination", "action"):
        v = args.get(k)
        if v:
            return str(v)[:70]
    return name


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# gemini-3.8-live (Nov 2026): smarter AND faster to first audio than the 2.5 native-audio
# model (0.5-0.9 s vs 0.4-2.2 s, better tool picks, gets maths/trick questions right without
# deep_think) and it issues session-resumption handles. No thinking_config allowed (1007).
LIVE_MODEL          = "models/gemini-3.8-live"
LIVE_FALLBACK_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_VOICE       = "Sulafat"      # warm, natural female voice (Nov 9 2026); set_voice / Settings change it
VOICES = {"sulafat": "Sulafat", "aoede": "Aoede", "achernar": "Achernar", "despina": "Despina", "leda": "Leda",
          "kore": "Kore", "puck": "Puck", "charon": "Charon", "fenrir": "Fenrir", "orus": "Orus"}
_RECONNECT = object()               # put on out_queue -> the session restarts (new voice)
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024  # 64ms at 16kHz — safe for all Windows audio drivers


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are BRINKO, a sharp and capable personal AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_last_memory_input = ""

def _update_memory_async(user_text: str, brinko_text: str) -> None:
    global _last_memory_input

    user_text   = (user_text   or "").strip()
    brinko_text = (brinko_text or "").strip()

    if len(user_text) < 5 or user_text == _last_memory_input:
        return
    _last_memory_input = user_text

    try:
        api_key = _get_api_key()
        if not should_extract_memory(user_text, brinko_text, api_key):
            return
        data = extract_memory(user_text, brinko_text, api_key)
        if data:
            update_memory(data)
            print(f"[Memory] ✅ {list(data.keys())}")
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ {e}")

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any installed application (desktop or Microsoft Store app) by name. "
            "Apps ALWAYS open in the background by default: minimized, without stealing "
            "focus, and never by typing into the Windows Start menu. Only pass "
            "background=false if the user explicitly wants the window shown in front "
            "right now ('open X and show it', 'bring X up'). If the result lists "
            "'closest installed apps', retry once with the obvious match. "
            "Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Name of the application (e.g. 'WhatsApp', 'Spotify', 'Bambu Studio')"
                },
                "background": {
                    "type": "BOOLEAN",
                    "description": "Default true (minimized, no focus steal). false only when the user wants it in front."
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "create_document",
        "description": (
            "Writes a real document — report, essay, summary, cheat sheet, plan, letter, guide, "
            "application — and builds it on screen with a live animation (blueprint → cursors → "
            "text typing in), then saves it as a PDF and/or Word file in Documents/BRINKO and opens it. "
            "Use for ANY 'make/write/create a PDF / Word doc / document / report about…'. Pass the "
            "user's request in their words with every detail they gave."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "What the document should be, in the user's words"},
                "format":  {"type": "STRING", "description": "pdf (default) | docx | both"},
                "title":   {"type": "STRING", "description": "Optional exact title"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "screensaver",
        "description": (
            "Starts BRINKO's full-screen screensaver: a flock of colourful arrow cursors and a "
            "realistic black hole (Interstellar-style), alternating. Any key or click exits. "
            "Use when the user says 'screensaver', 'show the black hole', 'cursor swarm' etc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode": {"type": "STRING", "description": "auto (default, alternates) | blackhole | cursors"},
            }
        }
    },
    {
        "name": "camera_powers",
        "description": (
            "Opens the camera in PORTAL mode: the user holds both hands up with thumb and index finger "
            "spread; the space between the four fingertips becomes a holographic window in which they see "
            "themselves in another look: Spider-Verse, Anime, Comic, Synthwave, Hologram, Thermal. Pulling the "
            "hands wide makes the look take over the whole screen; bringing the hands together closes it and "
            "the next opening shows the next look. Use for 'camera mode', 'finger frame', 'portal', "
            "'make me anime', 'spider-verse filter' etc. look= the one they asked for (switches it if the "
            "camera is already open). action=close to close it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "open (default) | close"},
                "look": {"type": "STRING", "description": "optional: spider-verse | anime | comic | synthwave | hologram | thermal"},
            }
        }
    },
    {
        "name": "deep_think",
        "description": (
            "Hands a BIG question to a stronger reasoning model that thinks before answering: "
            "multi-step planning (trips, schedules, budgets, workouts), detailed comparisons and "
            "buying decisions, long explanations, multi-part homework, debugging logic, strategy. "
            "Everyday maths, conversions and quick logic you answer yourself (step by step in your "
            "head). Say one short line first ('Let me think about that'), then speak the answer it "
            "returns in your own words — short, the key points only."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {"type": "STRING", "description": "The full question, with every detail the user gave"},
                "context":  {"type": "STRING", "description": "Optional relevant context from the conversation"},
            },
            "required": ["question"]
        }
    },
    {
        "name": "open_file",
        "description": (
            "Opens (or finds / shows in Explorer) a FILE or FOLDER on the user's PC from a "
            "loose description — no exact name or path needed. Searches Desktop, Documents, "
            "Downloads, Pictures, Videos, Music and OneDrive, understands file types "
            "('spreadsheet', 'pdf', 'photo', 'video', '3mf'), folders, and recency "
            "('my latest screenshot', 'the pdf I downloaded yesterday'). Use for ANY request "
            "to open/show/find a file, document, photo, video or folder. Pass the user's "
            "description in their own words (translated to English)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "What the user called it, e.g. 'my resume', 'budget spreadsheet', 'snake game folder', 'latest screenshot'"},
                "action":      {"type": "STRING", "description": "open (default) | reveal (show it selected in Explorer) | find (list matches without opening)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "web_search",
        "description": "Searches the web for any information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "Texts someone on WhatsApp (default), Telegram or Instagram. BRINKO writes the message "
            "itself from what the user wants to say (right language, tone, length). FLOW: first call "
            "with action=draft and intent=<what they want to say, in their words> → read the draft "
            "to the user and ask if it's good → on 'yes'/'send it' call action=send with the same "
            "receiver (the draft is remembered; no text needed). If they change something, draft "
            "again. Only use action=send straight away if the user dictated the exact words AND "
            "said to just send it. For a later time use scheduled_time='YYYY-MM-DD HH:MM'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":       {"type": "STRING", "description": "Contact name as saved in the app (e.g. 'Mama', 'Jake')"},
                "intent":         {"type": "STRING", "description": "What the user wants to say, in their own words — BRINKO writes the actual text"},
                "message_text":   {"type": "STRING", "description": "Only when the user dictated the exact words"},
                "platform":       {"type": "STRING", "description": "whatsapp (default) | telegram | instagram"},
                "action":         {"type": "STRING", "description": "draft (default — write and read back) | send"},
                "when":           {"type": "STRING", "description": "optional: send LATER at this local time, ISO like 2026-11-08T18:30 (use with action=send after the user approved the draft)"},
                "tone":           {"type": "STRING", "description": "Optional: funny, apologetic, flirty, formal…"},
                "language":       {"type": "STRING", "description": "Optional — otherwise matches the user/recipient"},
                "scheduled_time": {"type": "STRING", "description": "Optional: 'YYYY-MM-DD HH:MM' to send later"},
            },
            "required": ["receiver"]
        }
    },
    {
        "name": "music",
        "description": (
            "Controls music playback in the BACKGROUND — no window appears, no focus change. "
            "Media keys (play/pause/skip/volume) work immediately without opening any app. "
            "For Spotify song/playlist search, Spotify opens briefly then auto-minimizes. "
            "Use for: 'play music', 'pause', 'skip', 'volume up', 'play [song] on Spotify', "
            "'pause YouTube', 'play something while I work'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "play | pause | stop | skip | prev | volume_up | volume_down | mute | search | open_spotify | youtube_pause"},
                "query":   {"type": "STRING", "description": "Song, artist, or playlist name (for action=search/play with a query)"},
                "source":  {"type": "STRING", "description": "spotify | youtube (default: spotify)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "email_agent",
        "description": (
            "Writes excellent emails in the user's name — understands the goal, picks the right "
            "language (German for German recipients/contexts, with Sie/du), tone and length, and "
            "never invents facts (leaves [placeholders]). Without a connected mailbox it opens a "
            "ready-to-send Gmail compose window; with one it saves to Drafts. action=reply answers "
            "the latest email from someone using that email as context. read_recent lists the inbox. "
            "Only action=send sends directly, and only if the user explicitly asked to send now. "
            "After it returns, briefly tell the user what you wrote (subject + gist), don't read it all."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":   {"type": "STRING", "description": "draft (default) | reply | send | read_recent | learn_style"},
                "to":       {"type": "STRING", "description": "Recipient address/name — for reply: who sent the email to answer"},
                "topic":    {"type": "STRING", "description": "What the email should say, in the user's own words (keep word counts like '150 words')"},
                "tone":     {"type": "STRING", "description": "Optional: formal, friendly, apologetic, firm…"},
                "language": {"type": "STRING", "description": "Optional — auto-detected otherwise"},
                "subject":  {"type": "STRING", "description": "Optional subject override"},
                "body":     {"type": "STRING", "description": "Optional literal body (skips the writer)"},
                "count":    {"type": "INTEGER", "description": "Number of emails for read_recent"},
            },
            "required": []
        }
    },
    {
        "name": "phone_call",
        "description": (
            "Makes a REAL phone call where an AI voice agent holds the full conversation — "
            "handles back-and-forth dialogue, unexpected questions, and hold music. "
            "Use action=reserve for restaurant bookings (give phone, date, time, party size, name). "
            "Use action=call for any other call task (give phone and what to accomplish). "
            "Use action=status to check how an ongoing call is going. "
            "Requires a Bland.ai API key in config/api_keys.json."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":          {"type": "STRING",  "description": "reserve | call | status (default: call)"},
                "phone":           {"type": "STRING",  "description": "Phone number to call — include country code, e.g. +1 555 123 4567"},
                "task":            {"type": "STRING",  "description": "What to accomplish on the call (for action=call)"},
                "restaurant_name": {"type": "STRING",  "description": "Restaurant name (for action=reserve)"},
                "party_size":      {"type": "INTEGER", "description": "Number of people (for reserve, default 2)"},
                "date":            {"type": "STRING",  "description": "Date for the reservation, e.g. 'this Friday' or '2026-08-12'"},
                "time":            {"type": "STRING",  "description": "Desired time, e.g. '7pm' or '19:30'"},
                "name":            {"type": "STRING",  "description": "Name to use for the reservation"},
                "notes":           {"type": "STRING",  "description": "Special requests, e.g. 'window table, birthday surprise'"},
                "call_id":         {"type": "STRING",  "description": "Bland call ID (for action=status)"},
            },
            "required": []
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder (currently Windows only, uses Task Scheduler).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "schedule_task",
        "description": (
            "Schedules a real action (anything BRINKO can do) to actually EXECUTE at a future "
            "time — not just a popup reminder. Use when the user says 'at 8pm open Spotify', "
            "'schedule X for tomorrow', 'every morning check my email', etc. "
            "Windows only (Task Scheduler). For simple text reminders/popups, use reminder instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":     {"type": "STRING", "description": "create | list | cancel (default: create)"},
                "goal":       {"type": "STRING", "description": "What to do — exactly as you'd say it to BRINKO (e.g. 'open Spotify and play my top song')"},
                "date":       {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":       {"type": "STRING", "description": "Time in HH:MM (24h) format"},
                "recurrence": {"type": "STRING", "description": "'daily' | 'weekly' — omit for one-time"},
                "task_hint":  {"type": "STRING", "description": "Fragment of task name/description to match (for cancel)"},
            },
            "required": []
        }
    },
    {
        "name": "night_mode",
        "description": (
            "Queues and runs tasks autonomously while the user sleeps — no human in the loop. "
            "Use action=add to add a task to tonight's list. action=list to see what's queued. "
            "action=run to start running now (or when the user says 'good night', 'run while I sleep', etc.). "
            "action=clear to empty the queue. action=status to check if there's a morning report waiting. "
            "Tasks run with self-verification, 5 retries per step, and progressive error recovery. "
            "The PC is kept awake automatically. A morning report is delivered at next startup."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":   {"type": "STRING", "description": "add | list | run | clear | remove | status"},
                "goal":     {"type": "STRING", "description": "Task description (for add) — exactly as you'd say it to BRINKO"},
                "priority": {"type": "INTEGER", "description": "1–10, lower runs first (default 5)"},
                "index":    {"type": "INTEGER", "description": "Queue position to remove (for action=remove)"},
            },
            "required": []
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. US, TR"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool. "
            "After calling this tool, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command. NEVER route to agent_task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls the web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, any web-based task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | press | close"},
                "url":         {"type": "STRING", "description": "URL for go_to action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up or down for scroll"},
                "key":         {"type": "STRING", "description": "Key name for press action"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks requiring multiple different tools. "
            "Examples: 'research X and save to file', 'find and organize files'. "
            "DO NOT use for single commands. NEVER use for Steam/Epic — use game_updater."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":     {"type": "STRING", "description": "Complete description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high (default: normal)"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": (
            "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, "
            "find elements on screen. screen_find/screen_click use AI vision to locate and click "
            "any on-screen element by description (e.g. 'search bar', 'send button') — use these "
            "to interact INSIDE an app after opening it, not just to open the app."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Literal text to type or paste"},
                "generate":    {"type": "STRING", "description": "For type/smart_type: instead of 'text', describe what to write (e.g. 'a 300 word essay about gratitude') and BRINKO composes it at the moment of typing — use this for any original content instead of writing it into 'text' yourself"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click, e.g. 'search bar', 'play button'"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use agent_task, browser_control, or web_search for Steam/Epic. "
            "Note: full automation currently requires Windows."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
    "name": "shutdown_brinko",
    "description": (
        "Shuts down the assistant completely. "
        "Call this when the user expresses intent to end the conversation, "
        "close the assistant, say goodbye, or stop Brinko. "
        "The user can say this in ANY language."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
    },
    {
        "name": "stream_control",
        "description": (
            "Live-streaming workspace automation: opens OBS and Twitch, arranges "
            "them side-by-side, and can lock the screen to end a session. "
            "Use for ANY request about starting/setting up/ending a stream."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "start_session | open_obs | open_twitch | split_workspace | obs_scene | end_session (default: start_session)"},
                "channel": {"type": "STRING", "description": "Twitch channel name (optional)"},
                "scene":   {"type": "STRING", "description": "OBS scene name (for action=obs_scene, requires obs-websocket)"},
            },
            "required": []
        }
    },
    {
        "name": "ad_content",
        "description": (
            "Generates a REAL image (actual pixels, saved as a PNG — not just a text prompt), "
            "social-media ad creative concepts, AI image render prompts for external tools, "
            "and competitive ad-fatigue analysis. Use action=image whenever the user wants an "
            "actual picture/image/graphic made, not a description of one."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "image | concept | render_prompt | ad_fatigue (default: concept)"},
                "description": {"type": "STRING", "description": "What to actually generate an image of (for action=image), or what to write a render prompt for (for action=render_prompt)"},
                "style":       {"type": "STRING", "description": "Visual style hint (for image / render_prompt), e.g. 'photorealistic', 'watercolor', 'cyberpunk neon'"},
                "brief":       {"type": "STRING", "description": "Product/campaign brief (for concept)"},
                "platform":    {"type": "STRING", "description": "Instagram | TikTok | Facebook | X"},
                "count":       {"type": "INTEGER", "description": "Number of concepts (default 3)"},
                "brand":       {"type": "STRING", "description": "Brand or niche to analyze (for ad_fatigue)"},
                "save":        {"type": "BOOLEAN", "description": "Save text output to Desktop (default false). Images always save regardless."},
            },
            "required": []
        }
    },
    {
        "name": "ultra_mode",
        "description": (
            "Toggles Ultra Mode: strips filler/handoff chatter for maximum speed and "
            "switches heavy tools (code_helper, dev_agent) to a stronger model tier. "
            "Call when the user says things like 'turn on ultra mode', 'go turbo', "
            "'supercharge yourself', or asks whether it's currently on."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "enable | disable | status | toggle (default: toggle)"}
            },
            "required": []
        }
    },
    {
        "name": "background_jobs",
        "description": (
            "Lists the tasks BRINKO is currently running in the background (code builds, "
            "file processing, image generation, research…) and recently finished ones. "
            "Call when the user asks 'what are you working on', 'is it done yet', "
            "'status of my tasks'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "roll_call",
        "description": (
            "Has all 10 specialist agents (Dexter, Atlas, Friday, Vera, Zoey, Nova, "
            "Echo, Titan, Pixel, Sage) announce they're online and ready, with a "
            "matching visual sweep around BRINKO. Call when the user asks if the "
            "team/agents are ready, or asks for a systems/status check. "
            "After calling, stay completely silent — it announces itself."
        ),
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "look_at_screen",
        "description": (
            "See the user's screen RIGHT NOW (always-on, answers in ~1-2 s). Use whenever they refer to "
            "what they're looking at: 'what's this', 'what does this error mean', 'help me with this', "
            "'what am I looking at', 'read this', 'is this a good deal', 'summarize this page'. "
            "Prefer this over screen_process."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "question": {"type": "STRING", "description": "what the user wants to know about their screen"}}}
    },
    {
        "name": "recall",
        "description": (
            "Search your memory of PAST conversations (journal of everything said, plus daily summaries). "
            "Use for 'what did we talk about yesterday', 'what was that game I mentioned', 'what did you "
            "suggest last week', 'did I tell you about...'."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "query": {"type": "STRING", "description": "the question, including time words like 'yesterday'"}},
            "required": ["query"]}
    },
    {
        "name": "daily_briefing",
        "description": (
            "Morning briefing: weather (+ rain soon), what happened yesterday, new emails from people, "
            "local/interest headlines. Use for 'good morning', 'brief me', 'what's up today', 'catch me up'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "set_voice",
        "description": (
            "Change BRINKO's speaking voice (takes effect a second after you finish this sentence). "
            "Female: Sulafat (warm, default), Aoede (breezy, light), Achernar (soft), Despina (smooth), "
            "Leda (youthful), Kore (firm, clear). Male: Puck (friendly), Charon (deep), Fenrir, Orus. "
            "Use for 'change your voice', 'use a softer voice', 'go back to the male voice', 'what voices do you have'."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "voice": {"type": "STRING", "description": "one of the voice names, or 'list' to name the options"}}}
    },
    {
        "name": "proactive_settings",
        "description": (
            "Turn BRINKO's proactive notices on/off (battery low, rain soon, new emails from people, long "
            "sessions in one app) or change the long-session reminder. Use for 'stop interrupting me', "
            "'turn the reminders back on', 'remind me to take breaks every hour'."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "description": "on | off | status"},
            "long_session_minutes": {"type": "INTEGER", "description": "minutes in one app before a break nudge"}}}
    },
    {
        "name": "whatsapp_setup",
        "description": (
            "Background WhatsApp (WhatsApp Web, hidden browser). action=setup opens a window with the QR "
            "code the user scans ONCE with their phone; after that send_message on WhatsApp works in the "
            "background without taking over the screen. action=status, action=scheduled (list messages "
            "waiting to be sent later), action=cancel (cancel scheduled messages, optional receiver)."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "description": "setup | status | scheduled | cancel"},
            "receiver": {"type": "STRING", "description": "for cancel: only this person's scheduled messages"}}}
    },
    {
        "name": "code_project",
        "description": (
            "Work on the user's EXISTING code project: reads the project, edits the files, backs up the old "
            "versions, runs it/tests and auto-fixes errors, then opens VS Code. Use for 'add X to my game', "
            "'fix the bug in my project', 'make my website do Y', 'the code I have open in VS Code crashes, fix "
            "it'. (dev_agent is only for brand-new projects from scratch.) Runs in the background."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "instruction": {"type": "STRING", "description": "what to change/add/fix, in detail"},
            "project": {"type": "STRING", "description": "project folder name or path; empty = the folder open in VS Code"},
            "run_command": {"type": "STRING", "description": "optional command to check it, e.g. python main.py"}},
            "required": ["instruction"]}
    },
]


class BrinkoLive:

    def __init__(self, ui: BrinkoUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._speaking_lock = threading.Lock()
        self._greeted       = False
        self._play_ready    = threading.Event()   # set once the speaker stream is running
        self._wx_cache: dict = {}                 # weather fetched while connecting, for the greeting
        self._tool_tasks: set = set()
        from collections import deque
        self._recent = deque(maxlen=12)     # last exchanges, replayed after reconnects
        self._resume_handle = None          # session-resumption handle (3.x live models)
        self._live_model = LIVE_MODEL
        self._speaking_stopped_at = 0.0
        self._drop_audio = False                 # interrupt(): ignore the rest of the current answer
        self._typed_pending = ""                 # typed message waiting for its answer (journal)
        self.ui.on_text_command = self._on_text_command

        # Voice focus: standby while other apps play sound, so YouTube/Spotify
        # speech can't talk to BRINKO. Ctrl+Alt+B / orb tap / wake word opens it.
        from actions.voice_focus import get_voice_focus, HOTKEY_LABEL
        self.focus = get_voice_focus(self.ui)
        self._hotkey_label = HOTKEY_LABEL
        self.focus.on_change(self._on_focus_change)
        def _orb_tap():
            if self._is_speaking:
                self.interrupt()
            self.focus.activate("orb tap")
        self.ui.on_orb_tap = _orb_tap
        try:                                     # Settings -> Voice saves, then this restarts the session
            self.ui._win._settings_panel.on_voice_changed = self.restart_voice_session
        except Exception:
            pass

        # always-on screen awareness, memory summaries, proactive notices
        try:
            from actions import screen_watch
            screen_watch.start()
        except Exception as e:
            print(f"[BRINKO] ⚠️ screen watch: {e}")
        try:
            from memory import journal
            journal.ensure_summaries()
        except Exception as e:
            print(f"[BRINKO] ⚠️ journal: {e}")
        try:
            from actions.proactive import Proactive
            self.proactive = Proactive(self, self.ui)
            self.proactive.start()
        except Exception as e:
            print(f"[BRINKO] ⚠️ proactive: {e}")
        try:
            from actions import whatsapp_web
            whatsapp_web.start_scheduler(
                lambda it, res: self.proactive.notify(
                    f"The scheduled {it['platform']} message to {it['receiver']} - result: {res[:160]}"))
        except Exception as e:
            print(f"[BRINKO] ⚠️ message scheduler: {e}")

        try:
            import remote_server
            remote_server.register_brinko(self, TOOL_DECLARATIONS, _job_summary)
        except Exception as e:
            print(f"[BRINKO] ⚠️ phone link unavailable: {e}")

        jobs = get_jobs()
        jobs.on_change = self.ui.set_jobs
        jobs.on_finish = self._on_job_finished

        # Warm the app + file indexes so the first "open X" is instant.
        try:
            from actions.open_app import warm_cache
            threading.Thread(target=warm_cache, daemon=True).start()
            from actions.file_finder import warm_cache as warm_files
            warm_files()
        except Exception:
            pass

    def set_voice(self, want: str) -> str:
        """Save the new voice and restart the voice session once this answer has been spoken."""
        w = (want or "").strip().lower()
        if not w or w == "list":
            return ("Voices: female - Sulafat (warm), Aoede (breezy), Achernar (soft), Despina (smooth), "
                    "Leda (youthful), Kore (firm); male - Puck, Charon, Fenrir, Orus.")
        name = next((v for k, v in VOICES.items() if k in w), None)
        if not name:
            return f"I don't have a voice called '{want}'. Pick one of: " + ", ".join(VOICES.values())
        path = BASE_DIR / "config" / "api_keys.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            cfg["brinko_voice"] = name
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            return f"Couldn't save the voice setting: {e}"
        self.restart_voice_session()
        return f"Voice set to {name}. Say one short line in your current voice; the new voice starts right after."

    def restart_voice_session(self):
        """Reconnect (fresh, with the recent conversation replayed) after BRINKO stops talking."""
        def go():
            time.sleep(1.0)
            for _ in range(200):                    # wait (max ~20 s) until the current answer is spoken
                if not self._is_speaking:
                    break
                time.sleep(0.1)
            time.sleep(0.4)
            self._resume_handle = None             # new speech config -> new session
            self._switching = True
            loop, q = self._loop, getattr(self, "out_queue", None)
            if loop and q is not None:
                def put():
                    try:
                        q.put_nowait(_RECONNECT)
                    except Exception:
                        pass
                loop.call_soon_threadsafe(put)
        threading.Thread(target=go, daemon=True, name="VoiceSwitch").start()

    def interrupt(self):
        """Stop talking NOW (wake word / orb tap while BRINKO speaks): drop queued audio and the
        rest of this answer, open the mic right away."""
        self._drop_audio = True
        q = getattr(self, "audio_in_queue", None)
        if q is not None:
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break
        self._speaking_stopped_at = time.time() - 10      # no echo cooldown: playback is cut
        self.set_speaking(False)
        self.ui.write_log("SYS: ✋ Interrupted.")

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        if text and not text.startswith("["):
            self._typed_pending = text
        asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop)

    async def _send_text(self, text: str):
        """Typed / system text into the live session. Gemini 3.x live models reject
        send_client_content once audio is streaming (1007 'invalid argument' kills
        the session), so they get it as realtime text; the old native-audio model
        keeps the client-content path."""
        if "native-audio" in (self._live_model or ""):
            await self.session.send_client_content(turns={"parts": [{"text": text}]}, turn_complete=True)
        else:
            await self.session.send_realtime_input(text=text)

    def _idle_state(self) -> str:
        return "STANDBY" if self.focus.gated else "LISTENING"

    def _on_focus_change(self, gated: bool):
        with self._speaking_lock:
            speaking = self._is_speaking
        if not speaking and not self.ui.muted:
            self.ui.set_state("STANDBY" if gated else "LISTENING")

    def set_speaking(self, value: bool):
        # Called for every audio chunk — only touch the UI on an actual change,
        # otherwise we flood the Qt event loop with identical state signals.
        with self._speaking_lock:
            if self._is_speaking == value:
                return
            self._is_speaking = value
        if value:
            self.focus.keep_alive()
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.focus.keep_alive()
            self.ui.set_state(self._idle_state())

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop)

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        try:
            from memory.journal import recent_days_prompt
            days = recent_days_prompt()
            if days:
                parts.append(days)
        except Exception as e:
            print(f"[BRINKO] recent days: {e}")
        if self._recent and not self._resume_handle:
            # Without a resumption handle the voice session can't be resumed after a drop, so BRINKO re-reads
            # the last few exchanges and continues naturally.
            parts.append("[RECENT CONVERSATION — the connection was refreshed; continue naturally, "
                         "don't mention it]\n" + "\n".join(self._recent) + "\n")
        parts.append(sys_prompt)
        if is_ultra_enabled():
            parts.append(ULTRA_SYSTEM_BLOCK)

        # Load preferred voice from config (default Sulafat, a warm natural female voice)
        # and expressive than "Charon" (which is very deep/robotic).
        # Options: Aoede (warm female), Charon (deep male), Fenrir (strong male),
        # Kore (clear female), Puck (friendly, natural). Configurable in settings.
        try:
            _cfg_voice = json.loads((BASE_DIR / "config" / "api_keys.json").read_text()).get("brinko_voice") or DEFAULT_VOICE
        except Exception:
            _cfg_voice = DEFAULT_VOICE

        base_kwargs = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            # NOTE: no built-in {"google_search": {}} here — combined with
            # thinking_budget=0 the Live server fails every request with 1011.
            # Facts go through the web_search tool instead.
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            # a resumed session keeps the real conversation (3.x live models issue handles)
            session_resumption=types.SessionResumptionConfig(handle=self._resume_handle),
            # long conversations get summarised/slid instead of hitting the context limit
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()),
            # No thinking in the VOICE model: answers start ~1.1s sooner (median
            # 2.6s → 1.4s measured). Hard questions go to the deep_think tool.
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_cfg_voice,
                    )
                ),
                language_code="en-US",   # explicit hint — helps recognition accuracy significantly
            ),
        )

        # VAD tuning — these two values together caused the "th e hu ngr y" bug:
        # END_SENSITIVITY_HIGH treats the tiny natural pause *between syllables*
        # as end-of-speech, so each syllable was transcribed as a separate
        # utterance. Fix: LOW end sensitivity + a longer silence window so
        # Gemini waits for a genuine pause before deciding you've finished.
        # START_SENSITIVITY stays HIGH so it wakes up quickly when you speak.
        try:
            base_kwargs["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=900,     # 1500 felt sluggish; LOW sensitivity keeps words whole
                )
            )
        except Exception as e:
            print(f"[BRINKO] ⚠️ VAD tuning unavailable on this SDK version, using defaults: {e}")

        if "native-audio" in self._live_model:
            # the 2.5 native-audio model thinks by default; 3.x live models reject this field
            base_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.LiveConnectConfig(**base_kwargs)

    def _dispatch_sync(self, name: str, args: dict, speak=None) -> str:
        """Runs one tool synchronously and returns its result text. Used both
        inline (via an executor thread) and by background jobs."""
        ui, speak = self.ui, (speak or self.speak)

        if name == "open_app":
            return open_app(parameters=args, response=None, player=ui) or f"Opened {args.get('app_name')}."
        if name == "create_document":
            from actions.doc_maker import create_document
            return create_document(parameters=args, player=ui, speak=speak)
        if name == "screensaver":
            fx = getattr(ui, "fx", None)
            if not fx:
                return "The visual effects engine isn't available (PyQt6-WebEngine missing)."
            mode = str(args.get("mode") or "auto").lower()
            fx.open("screensaver.html", f"mode={mode if mode in ('auto', 'blackhole', 'cursors') else 'auto'}")
            return "Screensaver is on. Say nothing more — any key or click exits it."
        if name == "camera_powers":
            fx = getattr(ui, "fx", None)
            if not fx:
                return "The visual effects engine isn't available (PyQt6-WebEngine missing)."
            if str(args.get("action") or "open").lower() == "close":
                fx.close("/fx/camera")
                return "Camera closed."
            look = str(args.get("look") or "").strip()[:30]
            who = str(args.get("character") or "").strip()[:40]     # characters: hidden for now
            if who:
                import urllib.parse as _up
                fx.open("camera.html", "chars=1&c=" + _up.quote(who))
                return f"Camera portal is open in character mode with {who}. Esc closes it."
            if look and fx.is_open("/fx/camera"):
                fx.js("/fx/camera", "window.__brinko && __brinko.look(%s)" % json.dumps(look))
            else:
                import urllib.parse as _up
                fx.open("camera.html", ("c=" + _up.quote(look)) if look else "")
            return ("Camera portal is open" + (f" with the {look} look" if look else "") + ". Tell the user: hold "
                    "both hands up with thumb and index finger spread, the space between the fingertips becomes a "
                    "window into another look. Pull the hands wide to fill the screen, bring them together to "
                    "close it; opening again shows the next look (Spider-Verse, Anime, Comic, Synthwave, Hologram, "
                    "Thermal). Esc closes the camera.")
        if name == "code_project":
            from actions.code_project import code_project
            return code_project(args, player=ui)
        if name == "whatsapp_setup":
            from actions import whatsapp_web as WA
            act = str(args.get("action") or "status").lower()
            if act == "setup":
                ui.write_log("SYS: 📱 Opening WhatsApp Web - scan the QR code with your phone.")
                return WA.setup()
            if act == "scheduled":
                items = WA.pending()
                return ("Scheduled: " + "; ".join(f"{i['when'][11:16]} {i['when'][5:10]} to {i['receiver']}: {i['text'][:50]}" for i in items)) if items else "Nothing scheduled."
            if act == "cancel":
                return WA.cancel(str(args.get("receiver") or ""))
            return f"Background WhatsApp is {WA.status()}." + ("" if WA.is_ready() else " Say 'set up WhatsApp' to connect it.")
        if name == "look_at_screen":
            from actions.screen_watch import look_at_screen
            return look_at_screen(args)
        if name == "recall":
            from memory.journal import recall
            return recall(args)
        if name == "daily_briefing":
            from actions.proactive import daily_briefing
            return daily_briefing(args)
        if name == "set_voice":
            return self.set_voice(str(args.get("voice") or ""))
        if name == "proactive_settings":
            from actions.proactive import proactive_settings
            return proactive_settings(args)
        if name == "deep_think":
            from actions.deep_think import deep_think
            return deep_think(parameters=args, player=ui)
        if name == "open_file":
            from actions.file_finder import open_file
            return open_file(parameters=args, player=ui)
        if name == "weather_report":
            return weather_action(parameters=args, player=ui) or "Weather delivered."
        if name == "browser_control":
            return browser_control(parameters=args, player=ui) or "Done."
        if name == "file_controller":
            return file_controller(parameters=args, player=ui) or "Done."
        if name == "send_message":
            scheduled_time = str(args.get("scheduled_time", "") or "").strip()
            if scheduled_time:
                goal = (
                    f"Send a {args.get('platform','WhatsApp')} message to "
                    f"{args.get('receiver','')} saying: {args.get('message_text') or args.get('intent','')} "
                    f"(send it directly with action=send — the user already approved it)"
                )
                dt_parts = scheduled_time.split()
                sched_args = {"action": "create", "goal": goal, "date": dt_parts[0],
                              "time": dt_parts[1] if len(dt_parts) > 1 else "09:00"}
                try:
                    return (schedule_task(parameters=sched_args, player=ui, speak=speak)
                            or f"Scheduled message to {args.get('receiver')} at {scheduled_time}.")
                except Exception as e:
                    return f"Could not schedule the message: {e}"
            return (send_message(parameters=args, response=None, player=ui, session_memory=None)
                    or f"Message sent to {args.get('receiver')}.")
        if name == "music":
            return music(parameters=args, player=ui, speak=speak) or "Done."
        if name == "email_agent":
            return email_agent(parameters=args, player=ui, speak=speak) or "Done."
        if name == "phone_call":
            return phone_call(parameters=args, player=ui, speak=speak) or "Done."
        if name == "reminder":
            return reminder(parameters=args, response=None, player=ui) or "Reminder set."
        if name == "schedule_task":
            return schedule_task(parameters=args, player=ui, speak=speak) or "Done."
        if name == "night_mode":
            return night_mode(parameters=args, player=ui, speak=speak) or "Done."
        if name == "youtube_video":
            return youtube_video(parameters=args, response=None, player=ui) or "Done."
        if name == "file_processor":
            if not args.get("file_path") and ui.current_file:
                args["file_path"] = ui.current_file
            return file_processor(parameters=args, player=ui, speak=speak) or "Done."
        if name == "screen_process":
            threading.Thread(
                target=screen_process,
                kwargs={"parameters": args, "response": None, "player": ui, "session_memory": None},
                daemon=True,
            ).start()
            return "Vision module activated. Stay completely silent — vision module will speak directly."
        if name == "computer_settings":
            return computer_settings(parameters=args, response=None, player=ui) or "Done."
        if name == "desktop_control":
            return desktop_control(parameters=args, player=ui) or "Done."
        if name == "code_helper":
            return code_helper(parameters=args, player=ui, speak=speak) or "Done."
        if name == "dev_agent":
            return dev_agent(parameters=args, player=ui, speak=speak) or "Done."
        if name == "agent_task":
            from agent.task_queue import get_queue, TaskPriority
            priority_map = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL, "high": TaskPriority.HIGH}
            priority = priority_map.get(str(args.get("priority", "normal")).lower(), TaskPriority.NORMAL)
            task_id  = get_queue().submit(goal=args.get("goal", ""), priority=priority, speak=speak)
            return f"Task started in the background (ID: {task_id}). It will report when done."
        if name == "web_search":
            return web_search_action(parameters=args, player=ui) or "Done."
        if name == "computer_control":
            return computer_control(parameters=args, player=ui) or "Done."
        if name == "game_updater":
            return game_updater(parameters=args, player=ui, speak=speak) or "Done."
        if name == "flight_finder":
            return flight_finder(parameters=args, player=ui) or "Done."
        if name == "stream_control":
            return stream_control(parameters=args, player=ui, speak=speak) or "Done."
        if name == "ad_content":
            return ad_content(parameters=args, player=ui, speak=speak) or "Done."
        if name == "ultra_mode":
            r = ultra_mode(parameters=args, player=ui, speak=speak) or "Done."
            ui.set_ultra(is_ultra_enabled())
            return r
        if name == "background_jobs":
            return get_jobs().describe()
        if name == "roll_call":
            threading.Thread(target=ui.roll_call_sweep, daemon=True).start()
            speak(roll_call_text())
            return "Roll call complete. Stay silent — already announced."
        if name == "shutdown_brinko":
            ui.write_log("SYS: Shutdown requested.")
            speak("Goodbye, sir.")
            self.focus.shutdown()

            def _shutdown():
                import os
                time.sleep(1)
                os._exit(0)
            threading.Thread(target=_shutdown, daemon=True).start()
            return "Shutting down."
        return f"Unknown tool: {name}"

    def _should_background(self, name: str, args: dict) -> bool:
        if name not in BACKGROUND_TOOLS:
            return False
        action = str(args.get("action", "") or "").lower()
        return action not in _INLINE_ACTIONS.get(name, set())

    def _on_job_finished(self, job):
        ok = job.status == "done"
        self.ui.write_log(
            f"SYS: {'✓' if ok else '✗'} Job #{job.job_id} {job.tool} "
            f"{'finished' if ok else 'failed'} in {int(job.elapsed)}s"
        )
        self.speak(
            f"[BACKGROUND JOB {'FINISHED' if ok else 'FAILED'}] Job #{job.job_id} "
            f"({job.tool}: {job.summary}) took {int(job.elapsed)}s. Result: {job.result[:1500]}\n"
            f"Tell the user the outcome in one or two short sentences. Don't call any tool for this."
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"{persona_prefix(name)} 🔧 {name}  {args}")
        self.ui.flash_agent(persona_for(name))

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            return types.FunctionResponse(id=fc.id, name=name,
                                          response={"result": "ok", "silent": True})

        # ── slow tool → background job, answer immediately ──────────────
        if self._should_background(name, args):
            job = get_jobs().submit(name, _job_summary(name, args),
                                    lambda: self._dispatch_sync(name, args))
            result = (
                f"Started as background job #{job.job_id}. It is running now while the user "
                f"keeps working — say ONE short line that you're on it, then stop. "
                f"You'll get a [BACKGROUND JOB FINISHED] message with the result."
            )
            print(f"[BRINKO] 📤 {name} → background job #{job.job_id}")
            return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

        self.ui.set_state("THINKING")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: self._dispatch_sync(name, args))
        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state(self._idle_state())

        print(f"[BRINKO] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

    async def _handle_tool_call(self, tool_call):
        """Runs a batch of tool calls concurrently and replies once. Spawned
        as its own task so the receive loop never stalls on a slow tool."""
        try:
            fn_responses = await asyncio.gather(
                *(self._execute_tool(fc) for fc in tool_call.function_calls)
            )
            await self.session.send_tool_response(function_responses=list(fn_responses))
        except Exception as e:
            print(f"[BRINKO] ❌ tool batch failed: {e}")
            traceback.print_exc()

    def _send_morning_greeting(self):
        """Spoken once per launch right after connecting. BRINKO composes the
        line itself from real facts (time, name, weather, overnight report) so
        it's warm and different every time. NOTE: it must be sent as an
        instruction — sending the finished greeting text made the model treat
        it as something the USER said and reply to it instead."""
        from datetime import datetime

        now  = datetime.now()
        hour = now.hour
        part = ("early morning" if hour < 5 else "morning" if hour < 12 else
                "afternoon" if hour < 18 else "evening" if hour < 23 else "late night")

        mem = {}
        try:
            mem = load_memory()
        except Exception:
            pass

        def _mem_val(cat, key):
            v = (mem.get(cat) or {}).get(key)
            return (v.get("value") if isinstance(v, dict) else v) or None

        name = _mem_val("identity", "name")
        city = _mem_val("identity", "city")
        if not city:
            try:
                city = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8")).get("user_city") or None
            except Exception:
                pass

        facts = [f"It's {now.strftime('%A')} {part}, {now.strftime('%I:%M %p').lstrip('0')}."]
        if name:
            facts.append(f"The user's name is {name}.")
        if city:
            try:
                data = self._wx_cache.get(city) or get_weather_summary(city)
                if data:
                    facts.append(f"Weather in {city}: {format_weather_spoken(data)}")
            except Exception:
                pass
        try:
            night_report = read_morning_report()
            if night_report:
                facts.append(f"Overnight tasks report to mention briefly: {night_report}")
        except Exception:
            pass
        try:
            facts.append(f"Ultra mode is {'ON' if is_ultra_enabled() else 'off'}.")
        except Exception:
            pass

        self.speak(
            "[SESSION START — not something the user said] You just came online. "
            "Greet the user out loud with ONE warm, natural opening line (two short "
            "sentences max) in your BRINKO personality: friendly, a little witty, "
            "glad to see them. Fit it to the time of day; use their name if known; "
            "mention the weather only if it's given and only briefly. End by "
            "inviting them to say what they need. Vary it — never a stock phrase. "
            "Do NOT call any tools. Facts: " + " ".join(facts)
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            if msg is _RECONNECT:
                raise RuntimeError("restarting the voice session (voice change)")
            try:
                # google-genai SDK 1.x uses audio= for audio chunks
                await self.session.send_realtime_input(audio=msg)
            except TypeError:
                # Older SDK versions use media= (dict or list)
                try:
                    await self.session.send_realtime_input(media=[msg])
                except Exception:
                    await self.session.send_realtime_input(media=msg)

    def _resolve_input_device(self):
        """
        Picks the input device to record from and returns (index, info) —
        or (None, None) to let PortAudio use its own default. Checks for a
        manual override in config/audio_device.json first, since PortAudio's
        auto-picked "default" device is a common source of 'mic doesn't
        pick up' on Windows (it can silently choose a disabled/virtual
        device when several are installed).
        """
        try:
            override_path = BASE_DIR / "config" / "audio_device.json"
            if override_path.exists():
                data = json.loads(override_path.read_text(encoding="utf-8"))
                idx = data.get("input_device_index")
                if idx is not None:
                    info = sd.query_devices(idx)
                    return idx, info
        except Exception as e:
            print(f"[BRINKO] ⚠️ audio_device.json override ignored: {e}")

        try:
            idx = sd.default.device[0]
            if idx is not None and idx >= 0:
                return idx, sd.query_devices(idx)
        except Exception:
            pass
        return None, None

    async def _listen_audio(self):
        if sd is None:
            print("[BRINKO] ⚠️ sounddevice not available — voice input disabled, text input still works.")
            self.ui.write_log("SYS: Voice input disabled — 'sounddevice' isn't installed. Text input still works.")
            while True:
                await asyncio.sleep(3600)

        device_idx, device_info = self._resolve_input_device()
        if device_info:
            self.ui.write_log(
                f"SYS: Mic: {device_info.get('name', '?')} "
                f"({device_info.get('max_input_channels', '?')}ch, "
                f"{int(device_info.get('default_samplerate', 0))}Hz)"
            )
        else:
            self.ui.write_log(
                "SYS: ⚠️ No input device could be resolved — check Windows sound "
                "settings and that a microphone is set as default."
            )

        print("[BRINKO] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        self._mic_level    = 0.0
        self._last_level_t = time.time()
        self._speaking_stopped_at = 0.0   # timestamp when BRINKO last stopped speaking
        ECHO_COOLDOWN_SECS = 0.5           # room echo/reverb tail after playback ends
        _dropped_chunks    = [0]

        _chunks_sent = [0]

        # While in standby, keep the last ~1.5s of mic audio. When BRINKO wakes
        # ("Hey Jarvis, open Spotify" said in one breath), that pre-roll is sent
        # first so the words right after the wake phrase aren't lost.
        from collections import deque
        _preroll   = deque(maxlen=int(1.5 * SEND_SAMPLE_RATE / CHUNK_SIZE) + 1)
        _was_open  = [True]

        def _safe_enqueue(payload: dict):
            try:
                self.out_queue.put_nowait(payload)
                _chunks_sent[0] += 1
                if _chunks_sent[0] == 5:
                    # First 5 chunks got through — confirm to log that mic IS working
                    loop.call_soon_threadsafe(
                        lambda: self.ui.write_log("SYS: 🎤 Mic capturing audio — first chunks sent to Gemini.")
                    )
            except asyncio.QueueFull:
                _dropped_chunks[0] += 1
                if _dropped_chunks[0] % 50 == 1:
                    print(f"[BRINKO] ⚠️ Audio send queue full — {_dropped_chunks[0]} chunk(s) dropped.")

        def callback(indata, frames, time_info, status):
            if status:
                print(f"[BRINKO] ⚠️ Mic status: {status}")
            with self._speaking_lock:
                brinko_speaking = self._is_speaking

            raw = indata.tobytes()

            # Every chunk sends EITHER the real mic audio OR the same length of
            # pure silence. Sending nothing (muted / standby) made Gemini close
            # the session after a while ("client inactivity", error 1008).
            send = None
            if not self.ui.muted:
                echo_clear = (time.time() - self._speaking_stopped_at) > ECHO_COOLDOWN_SECS
                is_open = self.focus.mic_open()
                if not is_open:
                    _preroll.append(raw)
                elif not _was_open[0] and _preroll:
                    # just woke up: send what was said right before/around the wake phrase
                    for buffered in list(_preroll):
                        loop.call_soon_threadsafe(_safe_enqueue, {"data": buffered, "mime_type": "audio/pcm;rate=16000"})
                    _preroll.clear()
                _was_open[0] = is_open
                # BRINKO's own voice is blocked while it talks + a short echo tail.
                if echo_clear and not brinko_speaking and is_open:
                    send = raw
            loop.call_soon_threadsafe(
                _safe_enqueue,
                {"data": send if send is not None else bytes(len(raw)), "mime_type": "audio/pcm;rate=16000"},
            )

            # Level metering — lightweight audioop, throttled to ~5x/sec
            now_t2 = time.time()
            if now_t2 - self._last_level_t > 0.2:
                try:
                    if _HAS_AUDIOOP:
                        rms = audioop.rms(raw, 2)
                        self._mic_level = min(1.0, rms / 3000.0)
                    else:
                        import array
                        samples = array.array("h")
                        samples.frombytes(raw)
                        peak = max((abs(s) for s in samples[::4]), default=0)
                        self._mic_level = min(1.0, peak / 12000.0)
                except Exception:
                    pass
                self._last_level_t = now_t2

        try:
            with sd.InputStream(
                device=device_idx,
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[BRINKO] 🎤 Mic stream open")
                last_report = time.time()
                while True:
                    await asyncio.sleep(0.1)
                    self.ui.set_mic_level(self._mic_level)
                    # Once, a few seconds in, confirm whether we're actually
                    # seeing signal — silent forever almost always means
                    # wrong device or a muted/blocked mic at the OS level.
                    if last_report and time.time() - last_report > 4:
                        last_report = None
                        if self._mic_level < 0.02:
                            self.ui.write_log(
                                "SYS: Mic stream is open but input level is near zero. "
                                "If you're talking and still see this, check that this app "
                                "has microphone permission and the right device is selected "
                                "in Windows Sound settings (this may not be the mic you expect)."
                            )
        except Exception as e:
            print(f"[BRINKO] ❌ Mic: {e}")
            self.ui.write_log(f"SYS: ⚠️ Microphone failed to open: {e}")
            raise

    async def _receive_audio(self):
        print("[BRINKO] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data and not self._drop_audio:
                        self.audio_in_queue.put_nowait(response.data)

                    sru = getattr(response, "session_resumption_update", None)
                    if sru is not None and getattr(sru, "new_handle", None) and getattr(sru, "resumable", True) is not False:
                        self._resume_handle = sru.new_handle

                    if response.server_content:
                        sc = response.server_content

                        # Transcripts arrive as sub-word fragments ("th", "e hu",
                        # "ngry") whose own leading spaces mark real word breaks.
                        # Concatenate them RAW — stripping + joining with " "
                        # is what turned speech into "th e hu ngr y".
                        if sc.output_transcription and sc.output_transcription.text:
                            self.set_speaking(True)
                            out_buf.append(sc.output_transcription.text)

                        if sc.input_transcription and sc.input_transcription.text:
                            in_buf.append(sc.input_transcription.text)
                            self.focus.keep_alive()

                        if getattr(sc, "interrupted", False):
                            self._drop_audio = False
                            # drop queued speech so a barge-in takes effect immediately
                            while not self.audio_in_queue.empty():
                                self.audio_in_queue.get_nowait()

                        if sc.turn_complete:
                            # Speaking ends when PLAYBACK ends (see _play_audio),
                            # not when the server finishes sending — otherwise the
                            # mic reopens while BRINKO's voice is still coming out
                            # of the speakers and Gemini hears itself.
                            full_in = " ".join("".join(in_buf).split())
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._recent.append(f"User: {full_in[:300]}")
                            in_buf = []

                            full_out = " ".join("".join(out_buf).split())
                            if full_out:
                                self.ui.write_log(f"Brinko: {full_out}")
                                self._recent.append(f"BRINKO: {full_out[:300]}")
                            out_buf = []
                            self._drop_audio = False
                            try:
                                from memory import journal
                                journal.log_exchange(full_in or self._typed_pending, full_out,
                                                     "voice" if full_in else "typed")
                            except Exception as e:
                                print(f"[BRINKO] journal: {e}")
                            self._typed_pending = ""

                    if response.tool_call:
                        for fc in response.tool_call.function_calls:
                            print(f"[BRINKO] 📞 {fc.name}")
                        # Don't await here — keep receiving audio/transcripts
                        # while tools run.
                        t = asyncio.create_task(self._handle_tool_call(response.tool_call))
                        self._tool_tasks.add(t)
                        t.add_done_callback(self._tool_tasks.discard)

        except Exception as e:
            print(f"[BRINKO] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        if sd is None:
            while True:
                await asyncio.sleep(3600)

        print("[BRINKO] 🔊 Play started")

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()
        self._play_ready.set()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    # Playback drained → BRINKO has actually stopped talking.
                    # Only now reopen the mic (after the echo cooldown).
                    if self._is_speaking:
                        self._speaking_stopped_at = time.time()
                        self.set_speaking(False)
                    self.ui.set_out_level(0.0)
                    continue
                self.set_speaking(True)
                if _HAS_AUDIOOP:
                    try:
                        self.ui.set_out_level(min(1.0, audioop.rms(chunk, 2) / 6000.0))
                    except Exception:
                        pass
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[BRINKO] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    async def run(self):
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        # warm the greeting's weather while the first connection is being made
        def _prefetch_weather():
            try:
                city = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8")).get("user_city")
                try:
                    mem = load_memory()
                    v = (mem.get("identity") or {}).get("city")
                    city = (v.get("value") if isinstance(v, dict) else v) or city
                except Exception:
                    pass
                if city:
                    self._wx_cache[city] = get_weather_summary(city)
            except Exception as e:
                print(f"[BRINKO] weather prefetch: {e}")
        threading.Thread(target=_prefetch_weather, daemon=True).start()

        fails = 0
        while True:
            started = time.time()
            try:
                print("[BRINKO] 🔌 Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=self._live_model, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=200)

                    print("[BRINKO] ✅ Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: BRINKO online.")
                    self.ui.set_ultra(is_ultra_enabled())

                    if not self._greeted:
                        self._greeted = True
                        # Greet as soon as the speaker stream is actually running (a fixed 3 s
                        # wait was used before; 1.2 s was too tight and the first line got lost).
                        def _greet_when_ready():
                            self._play_ready.wait(timeout=6.0)
                            time.sleep(0.35)
                            for attempt in range(5):
                                if self.session and self._loop:
                                    self._send_morning_greeting()
                                    return
                                time.sleep(0.6)
                        threading.Thread(target=_greet_when_ready, daemon=True).start()
                        try:
                            from actions.wake_word import start_wake_word_listener
                            self._wake_thread = start_wake_word_listener(self.ui, brinko_instance=self)
                        except Exception as e:
                            print(f"[BRINKO] ⚠️ Wake word setup failed: {e}")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    
            except Exception as e:
                print(f"[BRINKO] ⚠️ {e}")
                traceback.print_exc()
                if getattr(self, "_switching", False):
                    pass
                elif time.time() - started < 30:
                    if self._resume_handle:
                        # an expired/invalid handle fails the connect: start fresh next time
                        print("[BRINKO] resume handle dropped")
                        self._resume_handle = None
                    elif self._live_model != LIVE_FALLBACK_MODEL and fails >= 2:
                        # new model unavailable (quota / retired preview): use the old one
                        print(f"[BRINKO] ⚠️ {self._live_model} keeps failing - using {LIVE_FALLBACK_MODEL}")
                        self._live_model = LIVE_FALLBACK_MODEL

            self.set_speaking(False)
            self.ui.set_state("THINKING")
            # Sessions end on their own (time limit, server hiccups): reconnect at once if the
            # last one ran a while; back off only when connecting itself keeps failing.
            if getattr(self, "_switching", False) or time.time() - started > 30:
                self._switching = False
                fails, delay = 0, 0.2
            else:
                fails += 1
                delay = min(10.0, 0.5 * 2 ** (fails - 1))
            print(f"[BRINKO] 🔄 Reconnecting in {delay:.1f}s...")
            await asyncio.sleep(delay)

def main():
    _ensure_single_instance()
    face_path = str(BASE_DIR / "face.png")
    if not Path(face_path).exists():
        face_path = ""  # ui.py falls back to a drawn orb if no face image found

    ui = BrinkoUI(face_path)

    try:
        from remote_server import start_remote_server
        start_remote_server(ui)
    except ImportError:
        msg = ("SYS: Phone remote access needs Flask — run "
               "'pip install -r requirements.txt' and restart BRINKO to enable it.")
        print(f"[Main] ⚠️ {msg}")
        ui.write_log(msg)
    except Exception as e:
        msg = f"SYS: Remote access failed to start: {e}"
        print(f"[Main] ⚠️ {msg}")
        ui.write_log(msg)

    def runner():
        ui.wait_for_api_key()
        brinko = BrinkoLive(ui)
        try:
            asyncio.run(brinko.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

    # Window closed → give other apps their volume back, then exit for real
    # (daemon threads + the Flask server would otherwise linger).
    try:
        from actions.voice_focus import _instance as _focus
        if _focus is not None:
            _focus.shutdown()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()

# from actions.cmd_control import cmd_control
# from actions.dev_agent import dev_agent
# from actions.computer_control import computer_control

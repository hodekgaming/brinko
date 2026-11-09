# actions/open_app.py
# Brinko — Cross-Platform App Launcher

import time
import os
import re
import subprocess
import platform
import shutil
import threading
import json
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_APP_ALIASES = {
    "whatsapp":           {"Windows": "WhatsApp",               "Darwin": "WhatsApp",            "Linux": "whatsapp"},
    "chrome":             {"Windows": "chrome",                 "Darwin": "Google Chrome",       "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                 "Darwin": "Google Chrome",       "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                "Darwin": "Firefox",             "Linux": "firefox"},
    "spotify":            {"Windows": "Spotify",                "Darwin": "Spotify",             "Linux": "spotify"},
    "chatgpt":            {"Windows": "ChatGPT",                "Darwin": "ChatGPT",             "Linux": "chatgpt"},
    "chat gpt":           {"Windows": "ChatGPT",                "Darwin": "ChatGPT",             "Linux": "chatgpt"},
    "claude":             {"Windows": "Claude",                 "Darwin": "Claude",              "Linux": "claude"},
    "gemini":             {"Windows": "Gemini",                 "Darwin": "Gemini",              "Linux": "gemini"},
    "obs":                {"Windows": "OBS Studio",             "Darwin": "OBS",                 "Linux": "obs"},
    "obs studio":         {"Windows": "OBS Studio",             "Darwin": "OBS",                 "Linux": "obs"},
    "twitch":             {"Windows": "Twitch",                 "Darwin": "Twitch",              "Linux": "twitch"},
    "vscode":             {"Windows": "code",                   "Darwin": "Visual Studio Code",  "Linux": "code"},
    "visual studio code": {"Windows": "code",                   "Darwin": "Visual Studio Code",  "Linux": "code"},
    "discord":            {"Windows": "Discord",                "Darwin": "Discord",             "Linux": "discord"},
    "telegram":           {"Windows": "Telegram",               "Darwin": "Telegram",            "Linux": "telegram"},
    "instagram":          {"Windows": "Instagram",              "Darwin": "Instagram",           "Linux": "instagram"},
    "tiktok":             {"Windows": "TikTok",                 "Darwin": "TikTok",              "Linux": "tiktok"},
    "notepad":            {"Windows": "notepad.exe",            "Darwin": "TextEdit",            "Linux": "gedit"},
    "calculator":         {"Windows": "calc.exe",               "Darwin": "Calculator",          "Linux": "gnome-calculator"},
    "terminal":           {"Windows": "cmd.exe",                "Darwin": "Terminal",            "Linux": "gnome-terminal"},
    "cmd":                {"Windows": "cmd.exe",                "Darwin": "Terminal",            "Linux": "bash"},
    "explorer":           {"Windows": "explorer.exe",           "Darwin": "Finder",              "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",           "Darwin": "Finder",              "Linux": "nautilus"},
    "paint":              {"Windows": "mspaint.exe",            "Darwin": "Preview",             "Linux": "gimp"},
    "word":               {"Windows": "winword",                "Darwin": "Microsoft Word",      "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                  "Darwin": "Microsoft Excel",     "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",               "Darwin": "Microsoft PowerPoint","Linux": "libreoffice --impress"},
    "vlc":                {"Windows": "vlc",                    "Darwin": "VLC",                 "Linux": "vlc"},
    "zoom":               {"Windows": "Zoom",                   "Darwin": "zoom.us",             "Linux": "zoom"},
    "slack":              {"Windows": "Slack",                  "Darwin": "Slack",               "Linux": "slack"},
    "steam":              {"Windows": "steam",                  "Darwin": "Steam",               "Linux": "steam"},
    "task manager":       {"Windows": "taskmgr.exe",            "Darwin": "Activity Monitor",    "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",           "Darwin": "System Preferences",  "Linux": "gnome-control-center"},
    "powershell":         {"Windows": "powershell.exe",         "Darwin": "Terminal",            "Linux": "bash"},
    "edge":               {"Windows": "msedge",                 "Darwin": "Microsoft Edge",      "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                  "Darwin": "Brave Browser",       "Linux": "brave-browser"},
    "obsidian":           {"Windows": "Obsidian",               "Darwin": "Obsidian",            "Linux": "obsidian"},
    "notion":             {"Windows": "Notion",                 "Darwin": "Notion",              "Linux": "notion"},
    "blender":            {"Windows": "blender",                "Darwin": "Blender",             "Linux": "blender"},
    "capcut":             {"Windows": "CapCut",                 "Darwin": "CapCut",              "Linux": "capcut"},
    "postman":            {"Windows": "Postman",                "Darwin": "Postman",             "Linux": "postman"},
    "figma":              {"Windows": "Figma",                  "Darwin": "Figma",               "Linux": "figma"},
}


# Only used as a LAST RESORT — i.e. after we've tried to launch the actual
# installed desktop app and it didn't confirm. Voice commands like
# "open chatgpt" should open the native app whenever it's installed; this
# map exists purely so the assistant still *does something useful* instead
# of failing silently when the app truly isn't installed.
_WEB_FALLBACK = {
    "chatgpt":   "https://chat.openai.com",
    "chat gpt":  "https://chat.openai.com",
    "claude":    "https://claude.ai",
    "gemini":    "https://gemini.google.com",
    "notion":    "https://notion.so",
    "twitch":    "https://twitch.tv",
    "figma":     "https://figma.com",
}


def _normalize(raw: str) -> str:
    system = platform.system()
    key    = raw.lower().strip()
    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(system, raw)
    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(system, raw)
    return raw


def _is_running(app_name: str) -> bool:
    if not _PSUTIL:
        return True
    app_lower = app_name.lower().replace(" ", "").replace(".exe", "")
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                proc_name = proc.info["name"].lower().replace(" ", "").replace(".exe", "")
                if app_lower in proc_name or proc_name in app_lower:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def _windows_app_paths_lookup(app_name: str) -> str | None:
    """Checks HKLM/HKCU ...CurrentVersion\\App Paths — the same registry
    key Windows itself uses to resolve friendly app names to real
    executables. Covers most installed desktop software."""
    try:
        import winreg
        candidates = [app_name, f"{app_name}.exe", app_name.replace(" ", "")]
        for cand in candidates:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        hive,
                        rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{cand}"
                    )
                    path, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    if path and Path(path.strip('"')).exists():
                        return path.strip('"')
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
    except ImportError:
        pass
    return None


# ── App index: Start Menu shortcuts + every Start app (incl. Store apps) ──────
# This is the same catalogue Windows Search uses, so we can launch anything it
# could find WITHOUT ever typing into the Start menu.

_INDEX: list[tuple[str, str, str]] | None = None     # (norm_name, display_name, target)
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE_FILE = Path(__file__).resolve().parent.parent / "config" / "app_index_v2.json"

# Brand/nickname → real app name (things people say vs. what's installed)
_NICKNAMES = {
    "bambu": "Bambu Studio", "bambulab": "Bambu Studio", "bamboolab": "Bambu Studio",
    "bamboo": "Bambu Studio", "bamboostudio": "Bambu Studio", "bambustudio": "Bambu Studio",
    "vscode": "Visual Studio Code", "code": "Visual Studio Code",
    "wordpad": "WordPad", "photos": "Photos", "camera": "Camera",
    "store": "Microsoft Store", "msstore": "Microsoft Store",
    "teams": "Microsoft Teams", "outlook": "Outlook",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _scan_shortcuts() -> list[tuple[str, str, str]]:
    out = []
    for base in (
        Path(os.environ.get("ProgramData", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ):
        if not base.exists():
            continue
        try:
            for lnk in base.rglob("*.lnk"):
                n = _norm(lnk.stem)
                if n and "uninstall" not in n:
                    out.append((n, lnk.stem, str(lnk)))
        except PermissionError:
            continue
    return out


def _known_folder(guid: str) -> str | None:
    """{6D809377-…} → C:\Program Files, etc. (SHGetKnownFolderPath)."""
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        u = uuid.UUID(guid)
        g = GUID(u.fields[0], u.fields[1], u.fields[2],
                 (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return None


def _target_for_appid(appid: str) -> str:
    r"""Desktop programs show up in Get-StartApps as '{KnownFolder}\rel\path.exe'.
    shell:AppsFolder can't launch those reliably — resolve to the real exe."""
    m = re.match(r"^\{([0-9A-Fa-f-]{36})\}\\(.+)$", appid)
    if m:
        base = _known_folder(m.group(1))
        if base:
            full = os.path.join(base, m.group(2))
            if os.path.exists(full):
                return full
    if re.match(r"^[a-z]+://", appid, re.I):
        return appid
    return "shell:AppsFolder\\" + appid


def _scan_start_apps() -> list[tuple[str, str, str]]:
    """Get-StartApps lists every launchable app, including Microsoft Store
    apps (Spotify, WhatsApp…) which have no .lnk file at all."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
             "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(r.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
    except Exception as e:
        print(f"[open_app] ⚠️ Get-StartApps failed: {e}")
        return []
    out = []
    for app in data:
        name, appid = str(app.get("Name") or ""), str(app.get("AppID") or "")
        n = _norm(name)
        if n and appid and "uninstall" not in n:
            out.append((n, name, _target_for_appid(appid)))
    return out


def _build_index() -> list[tuple[str, str, str]]:
    idx = _scan_start_apps() + _scan_shortcuts()   # Start apps first: they launch cleanest
    seen, uniq = set(), []
    for n, disp, tgt in idx:
        if n not in seen:
            seen.add(n)
            uniq.append((n, disp, tgt))
    try:
        _INDEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _INDEX_CACHE_FILE.write_text(json.dumps(uniq), encoding="utf-8")
    except Exception:
        pass
    return uniq


def _app_index() -> list[tuple[str, str, str]]:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            try:   # instant start from the last session's cache, refresh behind it
                _INDEX = [tuple(x) for x in json.loads(_INDEX_CACHE_FILE.read_text(encoding="utf-8"))]
                threading.Thread(target=_refresh_index, daemon=True).start()
            except Exception:
                _INDEX = _build_index()
        return _INDEX


def _refresh_index():
    global _INDEX
    fresh = _build_index()
    if fresh:
        with _INDEX_LOCK:
            _INDEX = fresh


def warm_cache():
    """Run once at startup on a background thread so the first launch is instant."""
    if platform.system() == "Windows":
        try:
            _app_index()
        except Exception:
            pass


_LEARNED_FILE = Path(__file__).resolve().parent.parent / "config" / "app_aliases.json"
_LEARNED: dict[str, str] | None = None


def _learned() -> dict[str, str]:
    global _LEARNED
    if _LEARNED is None:
        try:
            _LEARNED = json.loads(_LEARNED_FILE.read_text(encoding="utf-8"))
        except Exception:
            _LEARNED = {}
    return _LEARNED


def _remember(q: str, display: str):
    _learned()[q] = display
    try:
        _LEARNED_FILE.write_text(json.dumps(_LEARNED, indent=1), encoding="utf-8")
    except Exception:
        pass


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _heuristic_match(q: str, raw: str, idx) -> tuple[str, str] | None:
    for n, disp, tgt in idx:                       # exact
        if n == q:
            return disp, tgt
    starts = [(len(n), disp, tgt) for n, disp, tgt in idx
              if n.startswith(q) or (len(n) >= 4 and q.startswith(n))]
    if starts:                                     # "epic games" → "Epic Games Launcher"
        starts.sort()
        return starts[0][1], starts[0][2]

    # every word the user said appears in the app's name, in any order
    # ("games epic", "studio bambu", "office word")
    qw = [w for w in _words(raw) if w not in ("app", "the", "my", "application", "program")]
    if qw:
        hits = [(len(n), disp, tgt) for n, disp, tgt in idx
                if all(any(w == aw or (len(w) >= 4 and aw.startswith(w)) for aw in _words(disp)) for w in qw)]
        if hits:
            hits.sort()
            return hits[0][1], hits[0][2]

    contains = [(len(n), disp, tgt) for n, disp, tgt in idx if len(q) >= 4 and q in n]
    if contains:
        contains.sort()
        return contains[0][1], contains[0][2]

    import difflib                                 # fuzzy: "bamboo studio" → "Bambu Studio"
    by_norm = {n: (disp, tgt) for n, disp, tgt in idx}
    close = difflib.get_close_matches(q, list(by_norm), n=1, cutoff=0.78)
    if close:
        return by_norm[close[0]]
    return None


def _ai_match(raw: str, idx) -> tuple[str, str] | None:
    """Last resort: let the fast LLM work out what the user MEANT
    ("my 3d printer software" → Bambu Studio, "the game launcher" → Steam)
    from the list of apps that are actually installed."""
    try:
        from or_client import client
    except Exception:
        return None
    by_disp = {}
    for n, disp, tgt in idx:
        by_disp.setdefault(disp, tgt)
    names = sorted(by_disp)
    try:
        ans = client.chat(
            f"The user asked their Windows PC to open: \"{raw}\"\n"
            "Installed apps:\n" + "\n".join(names) + "\n\n"
            "Which ONE installed app do they mean? Consider nicknames, brands, "
            "what the app is for, and speech-to-text mistakes. Reply with the app "
            "name copied EXACTLY from the list, or NONE if nothing fits.",
            system="You map spoken requests to installed Windows apps. Answer with one app name or NONE.",
            max_tokens=40, temperature=0.0,
        ).strip().strip('"').strip("'").strip(".")
    except Exception as e:
        print(f"[open_app] ⚠️ AI match failed: {e}")
        return None
    if ans in by_disp:
        return ans, by_disp[ans]
    low = {k.lower(): k for k in by_disp}
    if ans.lower() in low:
        k = low[ans.lower()]
        return k, by_disp[k]
    return None


def _find_app(app_name: str, use_ai: bool = False) -> tuple[str, str] | None:
    """Best (display_name, target) for what the user said, or None."""
    q = _norm(app_name)
    if not q:
        return None
    nick = _NICKNAMES.get(q)
    if nick:
        q = _norm(nick)
    idx = _app_index()

    learned = _learned().get(q)
    if learned:
        for n, disp, tgt in idx:
            if disp == learned:
                return disp, tgt

    hit = _heuristic_match(q, nick or app_name, idx)
    if hit or not use_ai:
        return hit

    hit = _ai_match(app_name, idx)
    if hit:
        print(f"[open_app] 🧠 understood '{app_name}' as {hit[0]}")
        _remember(q, hit[0])
    return hit


def suggest_apps(app_name: str, n: int = 4) -> list[str]:
    import difflib
    idx = _app_index()
    by_norm = {nm: disp for nm, disp, _ in idx}
    q = _norm(app_name)
    picks = difflib.get_close_matches(q, list(by_norm), n=n, cutoff=0.45)
    if len(picks) < n and len(q) >= 3:   # word-level hits ("bamboo lab" → "Bambu Studio")
        for word in re.findall(r"[a-z0-9]{3,}", app_name.lower()):
            for nm in difflib.get_close_matches(word, list(by_norm), n=n, cutoff=0.4):
                if nm not in picks:
                    picks.append(nm)
            for nm, disp in by_norm.items():
                if nm not in picks and difflib.SequenceMatcher(None, word, nm[:len(word) + 1]).ratio() > 0.7:
                    picks.append(nm)
    return [by_norm[p] for p in picks[:n]]


# ── Background launching (Windows) ───────────────────────────────────────────
SW_SHOWNORMAL      = 1
SW_SHOWMINNOACTIVE = 7


def _shell_open(target: str, show: int) -> bool:
    ok = _shell_execute(target, show)
    if not ok and target.lower().startswith("shell:appsfolder"):
        # explorer can launch anything listed in the Apps folder
        try:
            subprocess.Popen(["explorer.exe", target], close_fds=True)
            return True
        except Exception:
            return False
    return ok


def _shell_execute(target: str, show: int) -> bool:
    """ShellExecuteW takes a show-command (minimized + don't activate),
    which os.startfile can't."""
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "open", str(target), None, None, show)
        return rc > 32
    except Exception as e:
        print(f"[open_app] ⚠️ ShellExecute failed: {e}")
        return False


def _popen(exe: str, minimized: bool) -> bool:
    try:
        si = None
        if minimized:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = SW_SHOWMINNOACTIVE
        subprocess.Popen([exe], startupinfo=si, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception:
        return False


def _snapshot_pids() -> set[int]:
    if not _PSUTIL:
        return set()
    try:
        return set(psutil.pids())
    except Exception:
        return set()


def _guard_focus(prev_hwnd: int, pre_pids: set[int], app_name: str, seconds: float = 9.0):
    """Many apps (Electron apps, Spotify, Discord…) ignore 'start minimized'
    and grab the foreground when their window appears. Watch for that, push
    the new window back down, and give focus back to what the user was in."""
    try:
        import ctypes
        from ctypes import wintypes
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        key      = re.sub(r"[^a-z0-9]", "", app_name.lower())
        my_pid   = os.getpid()

        def _pid(hwnd) -> int:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value

        def _proc_key(pid: int) -> str:
            if not _PSUTIL:
                return ""
            try:
                return re.sub(r"[^a-z0-9]", "", psutil.Process(pid).name().lower().replace(".exe", ""))
            except Exception:
                return ""

        def _restore_prev() -> bool:
            if not prev_hwnd or not user32.IsWindow(prev_hwnd):
                return False
            # Windows' foreground lock only lets the process that sent the
            # last input event change focus. A no-op key event (virtual key 0
            # does nothing in any app) makes that us.
            user32.keybd_event(0, 0, 0, 0)
            user32.keybd_event(0, 0, 2, 0)   # KEYEVENTF_KEYUP
            if user32.SetForegroundWindow(prev_hwnd):
                return True
            # Fallback: share the current foreground thread's input queue.
            fg   = user32.GetForegroundWindow()
            me   = kernel32.GetCurrentThreadId()
            fg_t = user32.GetWindowThreadProcessId(fg, None)
            attached = bool(fg_t and fg_t != me and user32.AttachThreadInput(me, fg_t, True))
            user32.BringWindowToTop(prev_hwnd)
            ok = bool(user32.SetForegroundWindow(prev_hwnd))
            if attached:
                user32.AttachThreadInput(me, fg_t, False)
            return ok

        deadline = time.time() + seconds
        pushed = 0
        while time.time() < deadline and pushed < 3:   # splash + main window is the usual max
            time.sleep(0.12)
            fg = user32.GetForegroundWindow()
            if not fg or fg == prev_hwnd:
                continue
            pid = _pid(fg)
            if pid == my_pid:
                continue
            pk = _proc_key(pid)
            name_hit = bool(key and pk and (key in pk or pk in key))
            if pid not in pre_pids or name_hit:
                user32.ShowWindow(fg, SW_SHOWMINNOACTIVE)
                _restore_prev()
                pushed += 1

        # A minimized window can still hold keyboard focus — make sure the
        # user's window really is in front again.
        if prev_hwnd and user32.GetForegroundWindow() != prev_hwnd:
            fg = user32.GetForegroundWindow()
            if fg and (user32.IsIconic(fg) or _pid(fg) not in pre_pids):
                _restore_prev()
    except Exception as e:
        print(f"[open_app] ⚠️ focus guard: {e}")


def _launch_windows(app_name: str, background: bool = True) -> bool:
    """Launches without ever touching the Start menu / Windows Search."""
    show = SW_SHOWMINNOACTIVE if background else SW_SHOWNORMAL

    def _open(target: str) -> bool:
        if target.lower().endswith(".exe"):
            return _popen(target, minimized=background)
        return _shell_open(target, show)

    # 0) URI schemes (ms-settings:, spotify:, …)
    if re.match(r"^[a-z][a-z0-9+.-]*:", app_name.lower()) and " " not in app_name:
        return _shell_open(app_name, show)

    # 1) Installed apps — Start apps (incl. Store apps) + Start Menu shortcuts
    hit = _find_app(app_name)
    if hit:
        print(f"[open_app] 🎯 matched '{app_name}' → {hit[0]}")
        if _open(hit[1]):
            return True

    # 2) Registry App Paths
    reg_path = _windows_app_paths_lookup(app_name)
    if reg_path and _open(reg_path):
        return True

    # 3) Plain executables on PATH (notepad.exe, calc.exe, cmd.exe …)
    exe = shutil.which(app_name)
    if exe and _open(exe):
        return True

    # 4) Work out what they MEANT ("my 3d printer app") from installed apps
    hit = _find_app(app_name, use_ai=True)
    if hit and _open(hit[1]):
        return True

    return False


def _launch_macos(app_name: str) -> bool:
    try:
        result = subprocess.run(["open", "-a", app_name], capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(["open", "-a", f"{app_name}.app"], capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] ⚠️ macOS Spotlight failed: {e}")
        return False



def _launch_linux(app_name: str) -> bool:
    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-"))
    )
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(["xdg-open", app_name], capture_output=True, timeout=5)
        return True
    except Exception:
        pass

    try:
        desktop_name = app_name.lower().replace(" ", "-")
        subprocess.run(["gtk-launch", desktop_name], capture_output=True, timeout=5)
        return True
    except Exception:
        pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params   = parameters or {}
    app_name = str(params.get("app_name", "")).strip()

    if not app_name:
        return "Please specify which application to open, sir."

    system   = platform.system()
    launcher = _OS_LAUNCHERS.get(system)

    if launcher is None:
        return f"Unsupported OS: {system}"

    # Background is the DEFAULT: apps open minimized and never steal focus.
    bg = params.get("background", True)
    background = not (str(bg).lower() in ("false", "0", "no")) if not isinstance(bg, bool) else bg
    normalized = _normalize(app_name)
    print(f"[open_app] 🚀 Launching: {app_name} → {normalized} ({system}"
          f"{', background' if background else ', foreground'})")

    if player:
        player.write_log(f"[open_app] {app_name}{' (background)' if background else ''}")

    def _launch(name: str) -> bool:
        if system == "Windows":
            return _launch_windows(name, background=background)
        return launcher(name)

    prev_hwnd, pre_pids = 0, set()
    if background and system == "Windows":
        try:
            import ctypes
            prev_hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass
        pre_pids = _snapshot_pids()

    def _ok() -> str:
        if background and system == "Windows":
            threading.Thread(target=_guard_focus, args=(prev_hwnd, pre_pids, app_name),
                             daemon=True).start()
            return (f"Opened {app_name} in the background (minimized) — "
                    f"the user's current window keeps focus.")
        return f"Opened {app_name}, sir."

    try:
        # Known aliases first (settings → ms-settings:, notepad → notepad.exe),
        # then whatever the user actually said.
        if _launch(normalized):
            return _ok()
        if normalized != app_name and _launch(app_name):
            return _ok()

        web_url = _WEB_FALLBACK.get(app_name.lower().strip())
        if web_url:
            try:
                from actions.browser_util import open_url
                open_url(web_url)
                return (
                    f"Couldn't find a {app_name} desktop app installed, sir — "
                    f"opened it in the browser instead."
                )
            except Exception:
                pass

        found = _find_app(app_name) if system == "Windows" else None
        if found:
            return (f"{found[0]} is installed but Windows didn't start it ({found[1]}). "
                    f"Tell the user it failed to launch — do NOT retry the same app.")

        hints = suggest_apps(app_name) if system == "Windows" else []
        if hints:
            return (f"No installed app called '{app_name}'. Closest installed apps: "
                    f"{', '.join(hints)}. If one of these is clearly what the user meant, "
                    f"call open_app again with that exact name; otherwise ask.")
        return f"Couldn't find an installed app called '{app_name}', sir."

    except Exception as e:
        print(f"[open_app] ❌ {e}")
        return f"Failed to open {app_name}, sir: {e}"

# actions/music.py
#
# Background music control — designed so BRINKO can play, pause, skip, and
# search music without stealing focus from whatever you're working on.
#
# HOW BACKGROUND WORKS:
#   Play / Pause / Skip / Volume → Windows media keys. These are OS-level
#   global shortcuts that work regardless of which app has focus. Spotify,
#   Windows Media Player, browser YouTube — all respond to them.
#
#   Specific song/playlist search on Spotify → opens Spotify via its URI
#   scheme (spotify:search:…), then immediately minimizes it using ctypes.
#   The window briefly appears for ~1s as Spotify loads the search, then
#   goes away. This is the limit of what's possible without the Spotify
#   Web API (which requires OAuth setup you'd have to do in a browser once).
#
#   YouTube control (play/pause only) → sends keyboard shortcuts to the
#   browser via browser_control's focused-tab path.
#
# HONEST LIMIT: Playing a SPECIFIC NAMED SONG without any window appearance
# requires the Spotify Web API. Without it, Spotify has to open briefly to
# navigate to the song. If you want truly invisible specific-track control,
# create a Spotify Developer app, add the client_id/secret to
# config/api_keys.json, and this module will use the Web API automatically.

import time
import sys
import json
import platform
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

_OS = platform.system()


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _media_key(key: str):
    """Sends a global media key — works without window focus on Windows."""
    try:
        import pyautogui
        pyautogui.press(key)
        return True
    except Exception as e:
        print(f"[Music] ⚠️ Media key {key} failed: {e}")
        return False


def _find_and_minimize(partial_title: str):
    """Finds a window containing partial_title and minimizes it."""
    if _OS != "Windows":
        return
    try:
        import ctypes
        SW_MINIMIZE = 6
        user32 = ctypes.windll.user32

        found = []
        def _enum_cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if partial_title.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        from ctypes import WINFUNCTYPE, c_bool, c_int, POINTER, c_void_p
        EnumWindowsProc = WINFUNCTYPE(c_bool, c_void_p, c_int)
        user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception as e:
        print(f"[Music] ⚠️ Minimize failed: {e}")


def _open_spotify(uri: str):
    """Opens a Spotify URI and minimizes Spotify after it loads."""
    try:
        if _OS == "Windows":
            subprocess.Popen(["start", uri], shell=True)
        elif _OS == "Darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(["xdg-open", uri])
        time.sleep(2.0)
        _find_and_minimize("Spotify")
    except Exception as e:
        print(f"[Music] ⚠️ Spotify open failed: {e}")


def _spotify_web_api_play(query: str, api_keys: dict) -> str | None:
    """
    Uses the Spotify Web API for truly invisible playback.
    Returns a result string if it worked, None if not configured.
    Requires: spotify_client_id, spotify_client_secret, spotify_refresh_token
    in config/api_keys.json.
    """
    client_id     = api_keys.get("spotify_client_id")
    client_secret = api_keys.get("spotify_client_secret")
    refresh_token = api_keys.get("spotify_refresh_token")
    if not (client_id and client_secret and refresh_token):
        return None

    try:
        import requests
        from base64 import b64encode

        # Get access token from refresh token
        creds = b64encode(f"{client_id}:{client_secret}".encode()).decode()
        tok_r = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=8,
        )
        tok_r.raise_for_status()
        access_token = tok_r.json()["access_token"]
        hdrs = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

        # Search for the track
        sr = requests.get(
            "https://api.spotify.com/v1/search",
            headers=hdrs, params={"q": query, "type": "track", "limit": 1}, timeout=8
        )
        tracks = sr.json().get("tracks", {}).get("items", [])
        if not tracks:
            return f"Couldn't find '{query}' on Spotify."

        uri = tracks[0]["uri"]
        name = tracks[0]["name"]
        artist = tracks[0]["artists"][0]["name"]

        # Start playback
        requests.put(
            "https://api.spotify.com/v1/me/player/play",
            headers=hdrs, json={"uris": [uri]}, timeout=8
        )
        return f"Playing '{name}' by {artist} in the background."

    except Exception as e:
        print(f"[Music] Spotify API error: {e}")
        return None


def _load_api_keys() -> dict:
    try:
        return json.loads((_get_base_dir() / "config" / "api_keys.json").read_text())
    except Exception:
        return {}


def music(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action  : play | pause | stop | skip | prev | volume_up | volume_down |
                  search | open_spotify | youtube_pause
        query   : song/artist/playlist name (for action=search)
        source  : "spotify" | "youtube" (default: spotify)
    """
    p      = parameters or {}
    action = p.get("action", "play").lower().strip()
    query  = p.get("query", "").strip()
    source = p.get("source", "spotify").lower().strip()

    if player:
        player.write_log(f"[Music] {action} {query or ''}")

    # ── Media key actions — completely background, no focus change ───────────
    if action in ("play",):
        _media_key("playpause")
        return "Resuming playback."

    if action in ("pause", "stop"):
        _media_key("playpause")
        return "Paused."

    if action == "skip":
        _media_key("nexttrack")
        return "Skipped to next track."

    if action == "prev":
        _media_key("prevtrack")
        return "Going back to previous track."

    if action == "volume_up":
        for _ in range(3):
            _media_key("volumeup")
        return "Volume up."

    if action == "volume_down":
        for _ in range(3):
            _media_key("volumedown")
        return "Volume down."

    if action == "mute":
        _media_key("volumemute")
        return "Audio muted/unmuted."

    # ── YouTube pause (browser tab) ──────────────────────────────────────────
    if action == "youtube_pause" or (action == "pause" and source == "youtube"):
        try:
            from actions.browser_control import browser_control
            browser_control({"action": "hotkey", "keys": "k"})
            return "YouTube paused."
        except Exception as e:
            return f"Could not pause YouTube: {e}"

    # ── Spotify search + play (opens Spotify, minimizes it) ─────────────────
    if action == "search" or (action == "play" and query):
        if not query:
            return "What would you like to play, sir?"

        api_keys = _load_api_keys()

        # Try Spotify Web API first (truly invisible if configured)
        web_result = _spotify_web_api_play(query, api_keys)
        if web_result:
            return web_result

        # Fall back: open Spotify with URI scheme, minimize after 2s
        if speak:
            speak("Opening Spotify and minimizing it, sir.")
        encoded = quote_plus(query)
        _open_spotify(f"spotify:search:{encoded}")
        # Give a moment then send play key
        time.sleep(0.5)
        _media_key("playpause")
        return (
            f"Searching Spotify for '{query}' and minimizing it, sir. "
            f"For fully invisible specific-song control, add your Spotify API credentials "
            f"(spotify_client_id / spotify_client_secret / spotify_refresh_token) to the settings."
        )

    if action == "open_spotify":
        _open_spotify("spotify:")
        return "Spotify opened and minimized, sir."

    return f"Unknown music action: '{action}'."

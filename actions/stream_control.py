# actions/stream_control.py
# Live-streaming workspace automation — opens OBS + Twitch, arranges them
# side-by-side on screen, and provides quick stream-session shortcuts.
#
# NOTE: this drives your existing default browser + OBS desktop app via the
# same window-snap / app-launch primitives already used elsewhere in the
# project (actions/open_app.py, actions/computer_settings.py). It does not
# touch OBS scenes/sources directly (that would need the obs-websocket
# plugin + a websocket client) — if you want scene switching, mic muting,
# or start/stop-recording from voice, wire up `obsws-python` and extend
# `_obs_ws_call()` below.

import time
import platform

from actions.open_app import open_app
from actions.computer_settings import snap_left, snap_right, lock_screen

try:
    # Reuses the existing cross-platform window-focus helper.
    from actions.computer_control import _focus_window
except Exception:
    def _focus_window(title: str) -> str:
        return "focus_window unavailable"

_OS = platform.system()


def _open_twitch(channel: str = "") -> str:
    url = (
        f"https://www.twitch.tv/{channel}"
        if channel else
        "https://www.twitch.tv/dashboard/stream"
    )
    try:
        from actions.browser_util import open_url
        open_url(url)
        return f"Opened Twitch: {url}"
    except Exception as e:
        return f"Could not open Twitch: {e}"


def _arrange_split(left_title: str, right_title: str) -> str:
    """Focuses each window in turn and snaps it to a side of the screen."""
    msgs = []
    for title, snap_fn in ((left_title, snap_left), (right_title, snap_right)):
        try:
            _focus_window(title)
            time.sleep(0.4)
            snap_fn()
            msgs.append(f"{title} snapped")
        except Exception as e:
            msgs.append(f"{title} snap failed: {e}")
    return "; ".join(msgs)


# --- Optional: obs-websocket hook (disabled unless the package + a running
# OBS with WebSocket server enabled are present). Lets you go beyond window
# management into actual scene control, e.g. action="obs_scene".
def _obs_ws_call(request_type: str, request_data: dict | None = None) -> str:
    try:
        import obsws_python as obs  # pip install obsws-python
    except ImportError:
        return "obs-websocket control not installed. Run: pip install obsws-python"
    try:
        cl = obs.ReqClient(host="localhost", port=4455, password="", timeout=3)
        result = cl.send(request_type, request_data or {})
        return f"OBS: {request_type} → {result}"
    except Exception as e:
        return f"OBS WebSocket call failed (is OBS open with WebSocket server enabled?): {e}"


def stream_control(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action  : start_session | open_obs | open_twitch | split_workspace |
                  obs_scene | end_session   (default: start_session)
        channel : Twitch channel name (optional)
        scene   : OBS scene name (for action="obs_scene")
    """
    p       = parameters or {}
    action  = p.get("action", "start_session").lower().strip()
    channel = p.get("channel", "").strip()
    scene   = p.get("scene", "").strip()

    if player:
        player.write_log(f"[Stream] {action}")

    if action == "open_obs":
        return open_app(parameters={"app_name": "OBS Studio"}, player=player)

    if action == "open_twitch":
        return _open_twitch(channel)

    if action == "split_workspace":
        return _arrange_split("OBS", channel or "Twitch")

    if action == "obs_scene":
        if not scene:
            return "Please specify a scene name, sir."
        return _obs_ws_call("SetCurrentProgramScene", {"sceneName": scene})

    if action == "end_session":
        if speak:
            speak("Ending the stream session, sir.")
        lock_screen()
        return "Stream session ended. Screen locked."

    if action == "start_session":
        results = [open_app(parameters={"app_name": "OBS Studio"}, player=player)]
        time.sleep(3.0)
        results.append(_open_twitch(channel))
        time.sleep(2.5)
        results.append(_arrange_split("OBS", channel or "Twitch"))
        return " | ".join(r for r in results if r)

    return f"Unknown stream_control action: '{action}'."

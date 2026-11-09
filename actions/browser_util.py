# actions/browser_util.py
# Shared "open this URL" helper. webbrowser.open() (Python stdlib) always
# uses the OS-registered default browser and has no way to prefer a
# specific one — several parts of the app used it directly, which quietly
# ignored the Brave preference even after browser_control.py's Playwright
# path respected it. This centralizes Brave-detection so every URL-opening
# call site (stream_control, open_app's web fallback, send_message,
# weather_report, and browser_control itself) is consistent.

import platform
import shutil
import subprocess
import webbrowser
from pathlib import Path

_BRAVE_BINARIES = {
    "Windows": ["brave.exe"],
    "Darwin":  ["brave browser", "brave"],
    "Linux":   ["brave-browser", "brave"],
}

_WINDOWS_FALLBACK_PATHS = (
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
)


def find_brave() -> str | None:
    system = platform.system()
    for binary in _BRAVE_BINARIES.get(system, []):
        path = shutil.which(binary)
        if path:
            return path

    if system == "Windows":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe"
                    )
                    val, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    val = val.strip('"')
                    if val and Path(val).exists():
                        return val
                except (FileNotFoundError, OSError):
                    continue
        except ImportError:
            pass

    for candidate in _WINDOWS_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate

    return None


def open_url(url: str) -> bool:
    """Opens a URL in Brave when it's installed, otherwise falls back to
    the OS default browser. Use this instead of webbrowser.open() directly
    anywhere a URL needs to be shown to the user."""
    brave = find_brave()
    if brave:
        try:
            subprocess.Popen([brave, url])
            return True
        except Exception:
            pass
    try:
        return webbrowser.open(url)
    except Exception:
        return False

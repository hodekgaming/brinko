import json
import platform
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "api_keys.json"
_PLATFORM_OS = {"Windows": "windows", "Darwin": "mac"}.get(platform.system(), "linux")


def get_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_os() -> str:
    """Returns: 'windows' | 'mac' | 'linux' (falls back to the real platform)."""
    return str(get_config().get("os_system") or _PLATFORM_OS).lower()


def is_windows() -> bool: return get_os() == "windows"
def is_mac()     -> bool: return get_os() == "mac"
def is_linux()   -> bool: return get_os() == "linux"

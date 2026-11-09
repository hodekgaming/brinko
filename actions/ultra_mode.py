# actions/ultra_mode.py
#
# Ultra Mode is a toggle, not a magic speed button — there's a hard floor on
# how fast a cloud LLM round-trip can be. What it actually changes:
#   1. The live system prompt gets an "ULTRA MODE" block: no filler chatter,
#      no pre-task narration, answer/act immediately, keep replies to one
#      sentence unless reporting data.
#   2. code_helper / dev_agent switch from the "flash" model tier to the
#      strongest available tier for better one-shot correctness (fewer
#      fix-retry loops end-to-end = faster in practice, even though any
#      single call may take a touch longer).
#   3. Cross-referenced by main.py at every reconnect, so toggling takes
#      effect on the next turn without restarting the app.

import json
import sys
from pathlib import Path
from threading import Lock


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


STATE_PATH = _get_base_dir() / "config" / "state.json"
_lock      = Lock()

ULTRA_SYSTEM_BLOCK = (
    "\n[ULTRA MODE — ACTIVE]\n"
    "- Skip all filler and pre-task narration. Call the tool immediately.\n"
    "- One sentence responses unless reporting data or asked for detail.\n"
    "- No hedging, no repeating the request back, no small talk unless the user starts it.\n"
    "- Assume the most capable, highest-effort interpretation of ambiguous requests.\n"
)


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    with _lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_ultra_enabled() -> bool:
    return bool(_load_state().get("ultra_mode", False))


def set_ultra_enabled(value: bool) -> None:
    state = _load_state()
    state["ultra_mode"] = bool(value)
    _save_state(state)


def ultra_mode(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action : enable | disable | status | toggle (default: toggle)
    """
    p      = parameters or {}
    action = p.get("action", "toggle").lower().strip()

    current = is_ultra_enabled()

    if action == "status":
        msg = f"Ultra mode is currently {'ON' if current else 'off'}, sir."
    elif action == "enable":
        set_ultra_enabled(True)
        msg = "Ultra mode engaged. No more filler, sir — let's move."
    elif action == "disable":
        set_ultra_enabled(False)
        msg = "Ultra mode disengaged. Back to the regular pace, sir."
    else:  # toggle
        set_ultra_enabled(not current)
        msg = (
            "Ultra mode engaged. No more filler, sir — let's move."
            if not current else
            "Ultra mode disengaged. Back to the regular pace, sir."
        )

    if player:
        player.write_log(f"[UltraMode] {'ON' if is_ultra_enabled() else 'off'}")

    return msg

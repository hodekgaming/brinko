# actions/cmd_control.py
#
# NOTE: This file did not exist in the original project — planner.py and
# executor.py both reference a "cmd_control" tool (for opening files /
# running simple natural-language system commands), but the actual module
# was never included. Without it, any agent_task plan step that used
# cmd_control would crash with ModuleNotFoundError. This is a minimal,
# safe implementation: it opens files/folders with the OS default handler
# and can run short shell commands when explicitly marked visible/safe.

import platform
import subprocess
import shlex
from pathlib import Path

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _open_path(path: str) -> str:
    """Opens a file or folder with the OS-default application."""
    p = Path(path).expanduser()
    if not p.exists():
        return f"Path not found: {path}"

    try:
        if _OS == "Windows":
            import os
            os.startfile(str(p))  # noqa: S606 - intentional, user-directed
        elif _OS == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return f"Opened: {p}"
    except Exception as e:
        return f"Could not open {p}: {e}"


def _resolve_target(task: str) -> str | None:
    """
    Very small heuristic resolver for common phrasing like:
    'open mechanical_engineering.txt on desktop with notepad'
    Falls back to None if nothing file-like is found.
    """
    lower = task.lower()
    desktop = Path.home() / "Desktop"

    if "desktop" in lower:
        # crude filename extraction: last token containing a dot
        for word in task.split():
            if "." in word and not word.startswith("http"):
                candidate = desktop / word.strip('"\'.,')
                if candidate.exists():
                    return str(candidate)
    return None


def cmd_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    parameters:
        task    : natural language description, e.g.
                  "open report.txt on desktop with notepad"
        path    : explicit path to open (preferred if given)
        command : explicit raw shell command to run (only used if 'visible'
                  is true — otherwise refused for safety, since this tool
                  is driven by an LLM planner and arbitrary command
                  execution is a real risk)
        visible : bool, must be true to allow raw 'command' execution
    """
    p       = parameters or {}
    task    = p.get("task", "").strip()
    path    = p.get("path", "").strip()
    command = p.get("command", "").strip()
    visible = bool(p.get("visible", False))

    if player:
        player.write_log(f"[cmd] {task or path or command}")

    if path:
        return _open_path(path)

    if task:
        target = _resolve_target(task)
        if target:
            return _open_path(target)
        return (
            f"I understood the task as: '{task}', but couldn't confidently "
            f"resolve a file/folder to open. Please give an explicit path."
        )

    if command:
        if not visible:
            return (
                "Refusing to run an arbitrary shell command without explicit "
                "confirmation. Pass visible=true if you really want this executed."
            )
        try:
            result = subprocess.run(
                shlex.split(command) if _OS != "Windows" else command,
                shell=(_OS == "Windows"),
                capture_output=True, text=True, timeout=30,
            )
            out = (result.stdout or result.stderr).strip()
            return out[:1500] if out else "Command ran with no output."
        except Exception as e:
            return f"Command failed: {e}"

    return "No task, path, or command provided."

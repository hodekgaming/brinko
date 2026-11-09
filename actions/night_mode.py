# actions/night_mode.py
#
# BRINKO Night Mode — runs a list of tasks fully autonomously while you
# sleep, with no human in the loop.
#
# What it does differently from a normal agent_task run:
#   - Prevents Windows from sleeping during task execution.
#   - Runs each task through the upgraded executor with night_mode=True:
#     more retries, progressive backoff, self-verification after each step,
#     non-critical step skipping instead of full abort.
#   - Writes a detailed JSON log to logs/ for every task attempted.
#   - Generates a plain-English morning report so BRINKO can brief the user
#     first thing — "Here's what I did overnight: X completed, Y failed."
#   - Uses a queue file (config/night_queue.json) so you can build up the
#     task list throughout the day before going to sleep.
#
# IMPORTANT HONEST LIMIT: BRINKO can only do things the computer is already
# set up to do — if a task needs credentials (Spotify login, a website with
# 2FA, etc.) that aren't already configured, it will fail. Build the list
# with tasks BRINKO can do right now, not things that need manual setup.

import sys
import json
import time
import platform
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


QUEUE_PATH  = _get_base_dir() / "config" / "night_queue.json"
LOGS_DIR    = _get_base_dir() / "logs"
REPORT_PATH = _get_base_dir() / "config" / "morning_report.json"


# ── Windows sleep prevention ────────────────────────────────────────────────

def _prevent_sleep():
    """Tells Windows the system is required to stay awake. Call at night-mode
    start; call _allow_sleep() when done."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ES_CONTINUOUS     = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        print("[NightMode] 🔋 Sleep prevention active.")
    except Exception as e:
        print(f"[NightMode] ⚠️ Could not prevent sleep: {e}")


def _allow_sleep():
    """Re-enables normal sleep behaviour after night mode finishes."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS only
        print("[NightMode] 🔋 Normal sleep behaviour restored.")
    except Exception as e:
        print(f"[NightMode] ⚠️ Could not restore sleep settings: {e}")


# ── Queue management ────────────────────────────────────────────────────────

def _load_queue() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_queue(q: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(q, indent=2), encoding="utf-8")


def add_task(goal: str, priority: int = 5) -> str:
    """Adds one task to the night queue. Priority 1 = first, 10 = last."""
    q = _load_queue()
    q.append({"goal": goal, "priority": priority, "added": datetime.now().isoformat()})
    q.sort(key=lambda x: x.get("priority", 5))
    _save_queue(q)
    return f"Added to tonight's queue (position {len(q)}): {goal}"


def list_tasks() -> str:
    q = _load_queue()
    if not q:
        return "The night queue is empty, sir."
    lines = [f"Night queue — {len(q)} task(s):"]
    for i, t in enumerate(q, 1):
        lines.append(f"  {i}. [{t.get('priority', 5)}] {t['goal']}")
    return "\n".join(lines)


def clear_tasks() -> str:
    _save_queue([])
    return "Night queue cleared, sir."


def remove_task(index: int) -> str:
    q = _load_queue()
    if index < 1 or index > len(q):
        return f"No task at position {index}, sir."
    removed = q.pop(index - 1)
    _save_queue(q)
    return f"Removed: {removed['goal']}"


# ── Core runner ─────────────────────────────────────────────────────────────

def _run_all(tasks: list[dict], speak: Callable | None, player) -> list[dict]:
    """Runs each task through the night-mode executor and returns a results list."""
    from agent.executor import AgentExecutor

    executor = AgentExecutor()
    results  = []

    for i, task in enumerate(tasks, 1):
        goal = task["goal"]
        print(f"\n[NightMode] ▶️  Task {i}/{len(tasks)}: {goal}")
        if player:
            player.write_log(f"[NightMode] Task {i}/{len(tasks)}: {goal}")

        step_log   = []
        started_at = datetime.now().isoformat()
        status     = "failed"
        summary    = ""

        try:
            summary = executor.execute(
                goal=goal,
                speak=None,       # stay silent during overnight tasks
                night_mode=True,  # enables verification + max retries
                step_log=step_log,
            )
            status = "completed"
            print(f"[NightMode] ✅ Task {i} done: {summary[:80]}")
        except Exception as e:
            summary = f"Uncaught error: {e}"
            print(f"[NightMode] ❌ Task {i} crashed: {e}")

        results.append({
            "goal":       goal,
            "status":     status,
            "summary":    summary,
            "steps":      step_log,
            "started_at": started_at,
            "ended_at":   datetime.now().isoformat(),
        })

        # Short pause between tasks to let rate limits breathe
        if i < len(tasks):
            time.sleep(8)

    return results


def _write_log(results: list[dict]) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"night_report_{ts}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return path


def _write_morning_report(results: list[dict]) -> None:
    """Writes a brief plain-English summary for the morning greeting."""
    done   = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] != "completed"]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps({
        "date":    datetime.now().strftime("%A, %B %d"),
        "done":    [r["goal"] for r in done],
        "failed":  [{"goal": r["goal"], "reason": r["summary"][:100]} for r in failed],
        "total":   len(results),
    }, indent=2), encoding="utf-8")


def read_morning_report() -> str | None:
    """Returns a spoken morning report string if there's an overnight report ready,
    or None if there's nothing to report. Called by main.py's morning greeting."""
    if not REPORT_PATH.exists():
        return None
    try:
        data  = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        REPORT_PATH.unlink()           # consume — only report once
        done  = data.get("done", [])
        failed = data.get("failed", [])
        date  = data.get("date", "last night")
        if not done and not failed:
            return None
        lines = [f"Overnight report for {date}:"]
        if done:
            lines.append(f"{len(done)} task(s) completed: {', '.join(done[:3])}{'...' if len(done) > 3 else ''}.")
        if failed:
            lines.append(f"{len(failed)} task(s) failed: {', '.join(r['goal'] for r in failed[:2])}.")
        return " ".join(lines)
    except Exception:
        return None


def start_night_mode(tasks: list[dict], speak: Callable | None, player) -> None:
    """Runs in a daemon thread — call and forget."""
    _prevent_sleep()
    try:
        results  = _run_all(tasks, speak, player)
        log_path = _write_log(results)
        _write_morning_report(results)

        done   = sum(1 for r in results if r["status"] == "completed")
        failed = len(results) - done
        print(f"\n[NightMode] 🌙 Done — {done} completed, {failed} failed. Log: {log_path}")

        if player:
            player.write_log(f"[NightMode] Finished — {done}/{len(results)} tasks completed.")
    finally:
        _allow_sleep()


# ── Public action interface ─────────────────────────────────────────────────

def night_mode(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action   : add | list | run | clear | remove | status
        goal     : task description (for action=add)
        priority : 1–10 (for add, lower runs first, default 5)
        index    : 1-based position (for remove)
    """
    p      = parameters or {}
    action = p.get("action", "list").lower().strip()

    if player:
        player.write_log(f"[NightMode] {action}")

    if action == "list":
        return list_tasks()

    if action == "clear":
        return clear_tasks()

    if action == "remove":
        idx = int(p.get("index", 1))
        return remove_task(idx)

    if action == "add":
        goal = p.get("goal", "").strip()
        if not goal:
            return "What task should I add to the queue, sir?"
        priority = int(p.get("priority", 5))
        return add_task(goal, priority)

    if action == "run":
        q = _load_queue()
        if not q:
            return "The night queue is empty, sir. Add tasks first."

        # Clear the queue before running so adding new tasks mid-run is safe
        _save_queue([])

        def _bg():
            start_night_mode(q, speak=None, player=player)
        threading.Thread(target=_bg, daemon=True, name="NightMode").start()

        n = len(q)
        return (
            f"Night mode running, sir. {n} task(s) queued — I'll work through them quietly "
            f"and have a full report ready when you're up. Good night."
        )

    if action == "status":
        if REPORT_PATH.exists():
            return "There's an overnight report waiting — it'll be in tomorrow's morning greeting, sir."
        return "No pending night report, sir."

    return f"Unknown night_mode action: '{action}'."

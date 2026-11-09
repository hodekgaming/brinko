"""Long-term conversation memory for BRINKO.

  * journal      - every exchange is appended to memory/journal/<day>.jsonl
  * learner      - every few exchanges (in the background, never on the voice path)
                   the fast model pulls out lasting facts -> memory_manager.update_memory
  * summaries    - each past day is condensed to 2-4 lines (+ which apps were used);
                   the last 3 days go into BRINKO's system prompt
  * recall()     - "what did we talk about yesterday / what was that game I mentioned"
                   searches the journals and answers from what was actually said
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
JOURNAL_DIR = BASE_DIR / "memory" / "journal"
SUMMARY_FILE = JOURNAL_DIR / "summaries.json"

LEARN_EVERY = 6          # exchanges per background fact-extraction pass
LEARN_IDLE_SECS = 150    # ...or this long after the last exchange

_lock = threading.Lock()
_pending: list[dict] = []
_last_add = 0.0
_learning = False


def _day(d: datetime | None = None) -> str:
    return (d or datetime.now()).strftime("%Y-%m-%d")


def _clean(t: str) -> str:
    return " ".join(str(t or "").split())[:1200]


# ── journal ───────────────────────────────────────────────────────────────
def log_exchange(user: str = "", brinko: str = "", source: str = "voice"):
    user, brinko = _clean(user), _clean(brinko)
    if not user and not brinko:
        return
    if user.startswith("[") and not brinko:          # internal instructions, not the user
        return
    rec = {"t": datetime.now().strftime("%H:%M"), "src": source, "user": user, "brinko": brinko}
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        with _lock, open(JOURNAL_DIR / f"{_day()}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Journal] write: {e}")
    global _last_add
    with _lock:
        _pending.append(rec)
        _last_add = time.time()
        due = len(_pending) >= LEARN_EVERY
    if due:
        _learn_async()


def read_day(day: str) -> list[dict]:
    f = JOURNAL_DIR / f"{day}.jsonl"
    out = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def _fmt(recs: list[dict], limit: int = 6000) -> str:
    lines = []
    for r in recs:
        if r.get("user"):
            lines.append(f"[{r.get('t', '')}] User: {r['user']}")
        if r.get("brinko"):
            lines.append(f"[{r.get('t', '')}] BRINKO: {r['brinko']}")
    text = "\n".join(lines)
    return text[-limit:]


# ── background fact learning ────────────────────────────────────────────────
def _learn_async():
    global _learning
    with _lock:
        if _learning or not _pending:
            return
        batch = list(_pending)
        _pending.clear()
        _learning = True

    def run():
        global _learning
        try:
            facts = _extract(batch)
            if facts:
                from memory.memory_manager import update_memory
                update_memory(facts)
                print(f"[Journal] learned: {json.dumps(facts, ensure_ascii=False)[:200]}")
        except Exception as e:
            print(f"[Journal] learn: {e}")
        finally:
            _learning = False

    threading.Thread(target=run, daemon=True, name="MemoryLearn").start()


def _extract(batch: list[dict]) -> dict:
    convo = _fmt(batch, 4000)
    if len(convo) < 40:
        return {}
    from memory.memory_manager import load_memory
    known = json.dumps(load_memory(), ensure_ascii=False)[:1500]
    from or_client import client
    raw = client.chat_json(
        "From this conversation between the user and their assistant BRINKO, extract LASTING facts about the "
        "user worth remembering for months: identity (name, age, city, school/job), preferences (favourite "
        "games, music, food, likes/dislikes), projects (what they're building/working on and its state), "
        "relationships (people, who they are), wishes (plans, things they want), notes (habits, schedule). "
        "Skip one-off commands, weather, searches, and anything already known. Short English values. "
        "Return {} if nothing new.\n"
        'Format: {"preferences": {"favorite_game": {"value": "Fortnite"}}, "projects": {"roblox_map": {"value": "building an obby map, wants to finish it this week"}}}\n\n'
        f"ALREADY KNOWN: {known}\n\nCONVERSATION:\n{convo}",
        max_tokens=1200)
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    return {k: v for k, v in (raw or {}).items() if k in valid and isinstance(v, dict) and v}


def tick():
    """Call every ~30 s: learns from a quiet conversation that didn't reach LEARN_EVERY."""
    with _lock:
        due = _pending and time.time() - _last_add > LEARN_IDLE_SECS
    if due:
        _learn_async()


# ── daily summaries ─────────────────────────────────────────────────────────
def _load_summaries() -> dict:
    try:
        return json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def summarize_day(day: str) -> str:
    recs = read_day(day)
    apps = {}
    try:
        from actions.screen_watch import usage_for
        apps = usage_for(day)
    except Exception:
        pass
    top = ", ".join(f"{a} {round(s / 3600, 1)}h" for a, s in list(apps.items())[:5] if s >= 600)
    if not recs and not top:
        return ""
    from or_client import client
    text = client.chat(
        "Summarise this day for the assistant's long-term memory in 2-4 short lines: what the user did, "
        "asked for, worked on or talked about, and anything left unfinished. Plain sentences, no bullet "
        "symbols, past tense, English.\n"
        f"Apps used (time in front): {top or 'unknown'}\n\nCONVERSATION:\n{_fmt(recs, 7000) or '(no conversation)'}",
        max_tokens=400)
    return " ".join((text or "").split())[:500]


def ensure_summaries(days_back: int = 7):
    """Summarise past days that have a journal or app usage but no summary yet (background)."""
    def run():
        sums = _load_summaries()
        changed = False
        for i in range(1, days_back + 1):
            day = _day(datetime.now() - timedelta(days=i))
            if day in sums:
                continue
            if not (JOURNAL_DIR / f"{day}.jsonl").exists() and not (BASE_DIR / "memory" / "activity" / f"{day}.json").exists():
                continue
            try:
                s = summarize_day(day)
                if s:
                    sums[day] = s
                    changed = True
            except Exception as e:
                print(f"[Journal] summary {day}: {e}")
        if changed:
            JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
            SUMMARY_FILE.write_text(json.dumps(sums, indent=1, ensure_ascii=False), encoding="utf-8")
    threading.Thread(target=run, daemon=True, name="DaySummaries").start()


def recent_days_prompt(n: int = 3) -> str:
    sums = _load_summaries()
    lines = []
    for i in range(1, 8):
        d = datetime.now() - timedelta(days=i)
        s = sums.get(_day(d))
        if s:
            lines.append(f"- {d.strftime('%a %d %b')}: {s}")
        if len(lines) >= n:
            break
    if not lines:
        return ""
    return "[RECENT DAYS - what happened before today; bring it up naturally when relevant]\n" + "\n".join(lines) + "\n"


# ── recall ─────────────────────────────────────────────────────────────────
_STOP = set("the a an and or to of in on at for is was were be been what which who when how did do does i me my you "
            "your we us it that this about with from again remember tell said say talked talk asked ask told "
            "last time thing things yesterday today week".split())


def _date_range(q: str) -> list[str]:
    ql = q.lower()
    now = datetime.now()
    if "yesterday" in ql:
        return [_day(now - timedelta(days=1))]
    if "today" in ql or "earlier" in ql:
        return [_day(now)]
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(days):
        if name in ql:
            back = (now.weekday() - i) % 7 or 7
            return [_day(now - timedelta(days=back))]
    n = 7 if "week" in ql else 60
    return [_day(now - timedelta(days=i)) for i in range(n)]


def recall(parameters: dict, player=None) -> str:
    q = str((parameters or {}).get("query") or "").strip()
    if not q:
        return "What should I try to remember?"
    days = _date_range(q)
    words = [w for w in re.findall(r"[a-zäöüß0-9']+", q.lower()) if w not in _STOP and len(w) > 2]
    hits = []
    for day in days:
        recs = read_day(day)
        for r in recs:
            text = f"{r.get('user', '')} {r.get('brinko', '')}".lower()
            score = sum(1 for w in words if w in text) if words else 1
            if score:
                hits.append((score, day, r))
    sums = _load_summaries()
    sum_hits = [(d, s) for d, s in sums.items() if d in days and (not words or any(w in s.lower() for w in words))]
    if not hits and not sum_hits:
        return "I don't have anything about that in my memory of our conversations."
    hits.sort(key=lambda h: (-h[0], h[1]), reverse=False)
    hits = sorted(hits[:14], key=lambda h: h[1])
    ctx = "\n".join(f"{d} {r.get('t', '')} | User: {r.get('user', '')} | BRINKO: {r.get('brinko', '')[:300]}" for _, d, r in hits)
    ctx += "\n" + "\n".join(f"{d} summary: {s}" for d, s in sum_hits[:7])
    from or_client import client
    ans = client.chat(
        f"The user asks: \"{q}\". Answer from these records of past conversations ONLY (dates are YYYY-MM-DD, "
        f"today is {_day()}). Two or three sentences, mention when it was. If the records don't answer it, say so.\n\n{ctx}",
        max_tokens=500)
    return (ans or "").strip() or "I couldn't find that."

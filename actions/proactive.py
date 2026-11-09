"""Proactive BRINKO: speaks up on its own when something is worth knowing.

Checks (every 30 s, each with its own rate limit):
  * battery low (20 / 10 / 5 %, urgent at 10)
  * rain starting within ~45 min (Open-Meteo, no key)
  * a new email from a real person (not newsletters / no-reply), if email is set up
  * a very long stretch in one app ("you've been in Fortnite for 2 hours")
Manners:
  * never while BRINKO or the user is talking (waits for a quiet moment)
  * standby (a game / video is playing) or quiet hours (23-7): only urgent things are
    spoken; the rest goes to the activity log
  * can be switched off: "stop the proactive stuff" -> proactive_settings(action=off)
Also: daily_briefing() for "good morning" / "brief me".
"""
from __future__ import annotations

import email
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CFG = BASE_DIR / "config" / "proactive.json"
_IGNORE_APPS = {"explorer", "python", "pythonw", "lockapp", "searchhost", "shellexperiencehost", "",
                "applicationframehost", "startmenuexperiencehost", "textinputhost"}


def load_cfg() -> dict:
    d = {"enabled": True, "quiet_start": 23, "quiet_end": 7, "long_session_min": 120}
    try:
        d.update(json.loads(CFG.read_text(encoding="utf-8")))
    except Exception:
        pass
    return d


def save_cfg(d: dict):
    CFG.write_text(json.dumps(d, indent=1), encoding="utf-8")


def _city() -> str:
    try:
        from memory.memory_manager import load_memory
        v = (load_memory().get("identity") or {}).get("city")
        v = v.get("value") if isinstance(v, dict) else v
        if v:
            return v
    except Exception:
        pass
    try:
        return json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8")).get("user_city") or ""
    except Exception:
        return ""


def _geo(city: str):
    import requests
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}, timeout=6).json()
    res = (r.get("results") or [None])[0]
    return (res["latitude"], res["longitude"], res.get("name", city)) if res else None


def rain_soon(city: str) -> str:
    """'' or 'Rain starts around 15:30 in Munich.' (within the next ~45 min, currently dry)"""
    import requests
    g = _geo(city)
    if not g:
        return ""
    lat, lon, name = g
    fc = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "minutely_15": "precipitation", "forecast_minutely_15": 6,
        "timezone": "auto"}, timeout=6).json()
    m = fc.get("minutely_15") or {}
    times, prec = m.get("time") or [], m.get("precipitation") or []
    if len(prec) < 4 or prec[0] > 0.05:
        return ""
    for t, p in zip(times[1:4], prec[1:4]):
        if p >= 0.2:
            return f"Rain starts around {t[-5:]} in {name}."
    return ""


def new_person_emails(since_uid: int | None) -> tuple[list[str], int | None]:
    """Unread emails from real people that arrived after since_uid -> ['Anna: Re: project', ...], newest uid."""
    from actions import email_agent as E
    cfg = E._load_config()
    if not cfg:
        return [], since_uid
    imap = E._connect_imap(cfg)
    try:
        E._imap_select(imap, "INBOX", readonly=True)
        st, data = imap.uid("search", None, "UNSEEN")
        uids = [int(x) for x in (data[0].split() if st == "OK" and data and data[0] else [])]
        if not uids:
            return [], since_uid
        newest = max(uids)
        if since_uid is None:                       # first run: just remember where we are
            return [], newest
        out = []
        for uid in [u for u in uids if u > since_uid][-5:]:
            st, md = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])")
            if st != "OK" or not md or not md[0]:
                continue
            h = md[0][1].decode(errors="ignore")
            frm = re.search(r"From:\s*(.+)", h)
            subj = re.search(r"Subject:\s*(.+)", h)
            fs = E._decode(frm.group(1)) if frm else ""
            if not _is_person(fs, h):
                continue
            name = re.sub(r"\s*<.*?>", "", fs).strip().strip('"') or fs
            out.append(f"{name}: {E._decode(subj.group(1)) if subj else '(no subject)'}")
        return out, newest
    finally:
        try:
            imap.logout()
        except Exception:
            pass


_PERSONAL = ("gmail.", "googlemail.", "outlook.", "hotmail.", "live.", "yahoo.", "icloud.", "me.com", "gmx.",
             "web.de", "t-online.", "proton", "aol.", "freenet.", "posteo.", "mail.de")
_ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _known_addresses() -> set[str]:
    try:
        from memory.memory_manager import load_memory
        return set(_ADDR.findall(json.dumps(load_memory()).lower()))
    except Exception:
        return set()


def _is_person(from_header: str, headers: str) -> bool:
    """A real person (personal mail provider or someone BRINKO knows), not a newsletter/shop/app."""
    m = _ADDR.search(from_header or "")
    addr = m.group(0).lower() if m else ""
    if not addr or re.search(r"no-?reply|newsletter|notification|mailer|marketing|^news@|^info@|^support@", addr):
        return False
    if re.search(r"List-Unsubscribe|Precedence:\s*(bulk|list)", headers or "", re.I):
        return False
    return addr in _known_addresses() or any(dom in addr.split("@")[1] for dom in _PERSONAL)


class Proactive:
    def __init__(self, brinko, ui):
        self.brinko, self.ui = brinko, ui
        self._queue: list[tuple[str, bool]] = []
        self._last = {}                 # check name -> last time run
        self._said = {}                 # key -> last time announced
        self._batt_level_warned = 101
        self._email_uid = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True, name="Proactive").start()

    # ── loop ──
    def _every(self, name: str, secs: float) -> bool:
        now = time.time()
        if now - self._last.get(name, 0) >= secs:
            self._last[name] = now
            return True
        return False

    def _run(self):
        time.sleep(20)                                    # let the greeting go first
        while True:
            try:
                from memory import journal
                journal.tick()
                cfg = load_cfg()
                if cfg.get("enabled", True):
                    self._checks(cfg)
                self._deliver(cfg)
            except Exception as e:
                print(f"[Proactive] {e}")
            time.sleep(30)

    def _checks(self, cfg):
        # battery
        try:
            import psutil
            b = psutil.sensors_battery()
            if b and not b.power_plugged:
                for lvl in (5, 10, 20):
                    if b.percent <= lvl < self._batt_level_warned:
                        self._batt_level_warned = lvl
                        self.notify(f"Battery is at {int(b.percent)} percent and not charging.", urgent=lvl <= 10)
                        break
            elif b and b.power_plugged:
                self._batt_level_warned = 101
        except Exception:
            pass
        # long stretch in one app
        try:
            from actions import screen_watch
            c = screen_watch.context()
            app = (c.get("app") or "").lower()
            mins = c.get("minutes_in_app", 0)
            lim = int(cfg.get("long_session_min", 120))
            if app not in _IGNORE_APPS and mins >= lim:
                key = f"long:{app}:{mins // lim}"
                if key not in self._said:
                    self._said[key] = time.time()
                    self.notify(f"The user has been in {c['app']} for {mins // 60} hours {mins % 60} minutes "
                                f"straight - suggest a short break, kindly and with a bit of humour.")
        except Exception:
            pass
        # rain within ~45 min (every 20 min, at most once per 3 h)
        if self._every("rain", 1200) and time.time() - self._said.get("rain", 0) > 3 * 3600:
            try:
                msg = rain_soon(_city())
                if msg:
                    self._said["rain"] = time.time()
                    self.notify(msg + " Mention it in case they're heading out.")
            except Exception as e:
                print(f"[Proactive] rain: {e}")
        # new email from a person (every 4 min)
        if self._every("mail", 240):
            try:
                mails, self._email_uid = new_person_emails(self._email_uid)
                if mails:
                    self.notify("New email" + ("s" if len(mails) > 1 else "") + ": " + "; ".join(mails)
                                + ". Tell them who it's from and what it's about, and offer to read it.")
            except Exception as e:
                print(f"[Proactive] mail: {e}")

    # ── delivery ──
    def notify(self, msg: str, urgent: bool = False):
        print(f"[Proactive] queued: {msg[:90]}")
        self._queue.append((msg, urgent))

    def _deliver(self, cfg):
        if not self._queue:
            return
        b = self.brinko
        h = datetime.now().hour
        qs, qe = int(cfg.get("quiet_start", 23)), int(cfg.get("quiet_end", 7))
        quiet = (h >= qs or h < qe) if qs > qe else (qs <= h < qe)
        try:
            from memory import journal
            talking_recently = time.time() - journal._last_add < 12
        except Exception:
            talking_recently = False
        busy = getattr(b, "_is_speaking", False) or talking_recently
        standby = bool(getattr(getattr(b, "focus", None), "gated", False))
        keep = []
        for msg, urgent in self._queue:
            if busy:
                keep.append((msg, urgent))
                continue
            if (standby or quiet or getattr(self.ui, "muted", False)) and not urgent:
                self.ui.write_log(f"SYS: 💡 {msg.split(' - ')[0].split('. Tell')[0].split('. Mention')[0]}")
                continue
            self.ui.write_log(f"SYS: 💡 {msg[:120]}")
            b.speak("[PROACTIVE NOTICE - not something the user said] Without being asked, tell the user in ONE "
                    f"short natural sentence: {msg} Don't call any tool for this.")
            busy = True                                   # one notice at a time
        self._queue = keep


# ── tools ──
def proactive_settings(parameters: dict, player=None) -> str:
    p = parameters or {}
    act = str(p.get("action") or "status").lower()
    cfg = load_cfg()
    if act in ("off", "disable", "stop"):
        cfg["enabled"] = False
    elif act in ("on", "enable", "start"):
        cfg["enabled"] = True
    if p.get("long_session_minutes"):
        cfg["long_session_min"] = max(30, int(p["long_session_minutes"]))
    if act != "status" or p.get("long_session_minutes"):
        save_cfg(cfg)
    return (f"Proactive notices are {'ON' if cfg['enabled'] else 'OFF'} (battery, rain soon, new emails from people, "
            f"long sessions after {cfg['long_session_min']} min; quiet {cfg['quiet_start']}:00-{cfg['quiet_end']}:00).")


def daily_briefing(parameters: dict = None, player=None) -> str:
    """Facts for a ~30 s spoken morning briefing; gathered in parallel."""
    city = _city()
    from memory.memory_manager import load_memory

    def weather():
        from actions.weather_report import get_weather_summary, format_weather_spoken
        w = get_weather_summary(city) if city else None
        txt = format_weather_spoken(w) if w else ""
        try:
            r = rain_soon(city) if city else ""
        except Exception:
            r = ""
        return (txt + (" " + r if r else "")).strip()

    def mail():
        try:
            from actions import email_agent as E
            cfg = E._load_config()
            if not cfg:
                return ""
            imap = E._connect_imap(cfg)
            E._imap_select(imap, "INBOX", readonly=True)
            since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            st, data = imap.search(None, "UNSEEN", "SINCE", since)
            n = len(data[0].split()) if st == "OK" and data and data[0] else 0
            imap.logout()
            people, _ = new_person_emails(0) if n else ([], None)
            return (f"{n} new unread emails since yesterday"
                    + (f"; from people: {'; '.join(people[:4])}" if people else " (none from people)"))
        except Exception as e:
            return ""

    def news():
        mem = load_memory()
        interests = []
        for cat in ("preferences", "projects"):
            for k, v in (mem.get(cat) or {}).items():
                val = v.get("value") if isinstance(v, dict) else v
                if val and len(interests) < 3 and any(w in k for w in ("game", "music", "sport", "team", "interest", "hobb")):
                    interests.append(str(val)[:40])
        from actions.web_search import _ddg_news
        out = []
        for q in (interests + [f"{city} news" if city else "world news"])[:2]:
            for r in _ddg_news(f"{q} news", max_results=2):
                out.append(f"{r['title']} ({r['date']})")
        return "; ".join(out[:4])

    def yesterday():
        try:
            from memory import journal
            day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            s = journal._load_summaries().get(day) or journal.summarize_day(day)
            return s or ""
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=4) as ex:
        fw, fm, fn, fy = ex.submit(weather), ex.submit(mail), ex.submit(news), ex.submit(yesterday)
        parts = {"weather": fw.result(), "email": fm.result(), "news": fn.result(), "yesterday": fy.result()}
    now = datetime.now()
    facts = [f"Today is {now.strftime('%A %d %B')}, {now.strftime('%H:%M')}."]
    facts += [f"{k.title()}: {v}" for k, v in parts.items() if v]
    return ("BRIEFING FACTS:\n" + "\n".join(facts) +
            "\n\nSpeak this as a warm, natural ~25-second briefing (like JARVIS in the morning): weather first, "
            "then anything from yesterday worth following up, emails, then one or two headlines. "
            "No lists, no reading URLs, skip anything empty.")

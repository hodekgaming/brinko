# waitlist.py — the FormProof waitlist (Supabase table public.waitlist).
#
# Read through the SECURITY DEFINER function public.brinko_waitlist(p_token, p_since), which
# only answers with the secret in config/api_keys.json["waitlist_token"] (the database keeps
# just its sha256 in private.reader_tokens). The table itself stays closed to the public key.
# A watcher polls every 60 s and announces new signups through Proactive.notify.

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
KEYS = BASE / "config" / "api_keys.json"
STATE = BASE / "config" / "waitlist_state.json"
POLL_S = 60


def _keys() -> dict:
    try:
        return json.loads(KEYS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def configured() -> bool:
    k = _keys()
    return bool(k.get("waitlist_token") and k.get("waitlist_supabase_url") and k.get("waitlist_supabase_key"))


def _call(since: str | None = None) -> dict:
    k = _keys()
    r = requests.post(k["waitlist_supabase_url"].rstrip("/") + "/rest/v1/rpc/brinko_waitlist",
                      headers={"apikey": k["waitlist_supabase_key"], "Content-Type": "application/json"},
                      json={"p_token": k["waitlist_token"], "p_since": since}, timeout=15)
    r.raise_for_status()
    return r.json()


def _ago(iso: str) -> str:
    try:
        d = (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return ""
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


def _who(row: dict) -> str:
    bits = [row.get("email") or "someone"]
    via = row.get("platform") or row.get("source")
    if via:
        bits.append(f"via {via}")
    if row.get("perk"):
        bits.append(f"perk {row['perk']}")
    return " ".join(bits)


def waitlist(args: dict | None = None) -> str:
    if not configured():
        return "The waitlist isn't connected on this PC (waitlist_token / Supabase URL missing in config/api_keys.json)."
    try:
        d = _call()
    except Exception as e:
        return f"Couldn't reach the waitlist: {str(e)[:150]}"
    lines = [f"Waitlist: {d['total']} people total, {d['today']} today, {d['last_7_days']} in the last 7 days."]
    latest = d.get("latest") or []
    if latest:
        lines.append("Newest: " + "; ".join(f"{_who(r)} ({_ago(r['created_at'])})" for r in latest[:5]))
    return "\n".join(lines)


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def start_watcher(brinko, ui) -> threading.Thread | None:
    if not configured():
        return None

    def loop():
        st = _load_state()
        while True:
            try:
                if not st.get("last"):
                    d = _call()
                    lat = d.get("latest") or []
                    st["last"] = lat[0]["created_at"] if lat else datetime.now(timezone.utc).isoformat()
                    STATE.write_text(json.dumps(st), encoding="utf-8")
                else:
                    d = _call(st["last"])
                    new = d.get("latest") or []
                    if new:
                        st["last"] = new[0]["created_at"]
                        STATE.write_text(json.dumps(st), encoding="utf-8")
                        who = "; ".join(_who(r) for r in new[:5])
                        n = len(new)
                        msg = (f"New waitlist signup{'s' if n > 1 else ''} ({n}): {who}. Waitlist is now at "
                               f"{d['total']}. Tell the user happily in one short sentence (say the email's name part, "
                               f"not the whole address).")
                        ui.write_log(f"SYS: 🎉 {n} new waitlist signup{'s' if n > 1 else ''} — total {d['total']}")
                        p = getattr(brinko, "proactive", None)
                        if p:
                            p.notify(msg, urgent=False)
            except Exception as e:
                print(f"[Waitlist] {str(e)[:120]}")
            time.sleep(POLL_S)

    t = threading.Thread(target=loop, daemon=True, name="waitlist-watch")
    t.start()
    return t

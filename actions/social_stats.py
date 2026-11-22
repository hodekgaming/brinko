# social_stats.py — the user's public Instagram / YouTube / TikTok numbers.
#
# No logins or API keys: TikTok's public profile JSON, YouTube's channel header and
# Instagram's profile meta (both read in a hidden Playwright Chromium). Handles live in
# config/social.json; every check is saved to memory/social_history.json so BRINKO can
# say how much each number grew since the last check / yesterday / last week.

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
CFG = BASE / "config" / "social.json"
HIST = BASE / "memory" / "social_history.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/142.0 Safari/537.36")
PLATFORMS = ("instagram", "youtube", "tiktok")
_lock = threading.Lock()


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _num(s) -> int | None:
    """'91.2M' / '1,030' / '528M' / '1K' → int."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMB])?", s, re.I)
    if not m:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1)
    return int(float(m.group(1)) * mult)


def _clean(handle: str) -> str:
    h = (handle or "").strip()
    h = re.sub(r"^https?://(www\.)?(instagram|tiktok|youtube)\.com/", "", h, flags=re.I)
    return h.strip("/").lstrip("@").split("/")[0].split("?")[0]


# ── fetchers ────────────────────────────────────────────────────────────────
def _tiktok(handle: str) -> dict:
    r = requests.get(f"https://www.tiktok.com/@{handle}", headers={"User-Agent": UA, "Accept-Language": "en-US"},
                     timeout=15)
    m = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("TikTok didn't return the profile")
    info = json.loads(m.group(1))["__DEFAULT_SCOPE__"]["webapp.user-detail"].get("userInfo") or {}
    st = info.get("statsV2") or info.get("stats") or {}
    if not st:
        raise RuntimeError(f"no TikTok account @{handle}")
    return {"followers": _num(st.get("followerCount")), "following": _num(st.get("followingCount")),
            "likes": _num(st.get("heartCount") or st.get("heart")), "videos": _num(st.get("videoCount"))}


def _browser_pages(jobs: dict) -> dict:
    """jobs = {platform: handle} for instagram/youtube → {platform: stats or {'error'}}."""
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            ctx = b.new_context(locale="en-US", user_agent=UA)
            ctx.add_cookies([{"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"}])
            for plat, h in jobs.items():
                pg = ctx.new_page()
                try:
                    if plat == "youtube":
                        url = f"https://www.youtube.com/@{h}" if not h.startswith("UC") else f"https://www.youtube.com/channel/{h}"
                        pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                        txt = pg.locator("yt-page-header-renderer, #page-header").first.inner_text(timeout=10000)
                        subs = re.search(r"([\d.,]+[KMB]?)\s+subscribers", txt)
                        vids = re.search(r"([\d.,]+[KMB]?)\s+videos", txt)
                        if not subs:
                            raise RuntimeError(f"no YouTube channel @{h}")
                        out[plat] = {"subscribers": _num(subs.group(1)), "videos": _num(vids.group(1)) if vids else None}
                        try:
                            pg.goto(url + "/videos", wait_until="domcontentloaded", timeout=30000)
                            pg.locator("ytd-rich-item-renderer").first.wait_for(timeout=10000)
                            rec = []
                            for t in pg.locator("ytd-rich-item-renderer").all_inner_texts()[:8]:
                                ln = [x.strip() for x in t.split("\n") if x.strip()]
                                ln = [x for x in ln if not re.fullmatch(r"[\d:]+|LIVE|SHORTS", x)]
                                v = next((x for x in ln if re.fullmatch(r"[\d.,]+[KMB]?( views)?", x)), None)
                                age = next((x for x in ln if re.search(r"ago$", x)), "")
                                if ln and v:
                                    rec.append({"title": ln[0][:70], "views": _num(v), "age": age})
                            out[plat]["recent"] = rec
                        except Exception:
                            pass
                    else:
                        pg.goto(f"https://www.instagram.com/{h}/", wait_until="domcontentloaded", timeout=30000)
                        pg.wait_for_timeout(2500)
                        meta = re.search(r'content="([\d.,KMB]+) Followers, ([\d.,KMB]+) Following, ([\d.,KMB]+) Posts',
                                         pg.content())
                        if not meta:
                            raise RuntimeError(f"Instagram didn't show @{h} (private, wrong name or rate-limited)")
                        out[plat] = {"followers": _num(meta.group(1)), "following": _num(meta.group(2)),
                                     "posts": _num(meta.group(3))}
                        try:
                            pg.goto(f"https://www.instagram.com/{h}/reels/", wait_until="networkidle", timeout=30000)
                            html, rec, seen = pg.content(), [], set()
                            for m in re.finditer(r'"play_count":(\d+)', html):
                                seg = html[max(0, m.start() - 2500):m.start()]
                                code = (re.findall(r'"code":"([\w-]+)"', seg) or [""])[-1]
                                key = (code, m.group(1))
                                if key in seen:
                                    continue
                                seen.add(key)
                                rec.append({"title": f"reel {code}" if code else "reel", "views": int(m.group(1)), "age": ""})
                                if len(rec) >= 8:
                                    break
                            out[plat]["recent"] = rec
                        except Exception:
                            pass
                except Exception as e:
                    out[plat] = {"error": str(e)[:160]}
                finally:
                    pg.close()
        finally:
            b.close()
    return out


def _tiktok_views(handle: str) -> list:
    """TikTok refuses its video list to hidden browsers, so a real Chrome/Edge window is opened
    OFF-SCREEN for a few seconds and the page's own item_list responses are read."""
    from playwright.sync_api import sync_playwright
    items = []
    with sync_playwright() as p:
        b = None
        for ch in ("chrome", "msedge"):
            try:
                b = p.chromium.launch(channel=ch, headless=False, args=[
                    "--window-position=-2400,0", "--window-size=1100,900",
                    "--disable-blink-features=AutomationControlled"])
                break
            except Exception:
                continue
        if b is None:
            return []
        try:
            ctx = b.new_context(locale="en-US")
            resp = []
            ctx.on("response", lambda r: resp.append(r) if "item_list" in r.url else None)
            pg = ctx.new_page()
            pg.goto(f"https://www.tiktok.com/@{handle}", wait_until="domcontentloaded", timeout=30000)
            for _ in range(16):
                pg.wait_for_timeout(500)
                if any(r.status == 200 for r in resp) and len(resp) >= 1 and _ > 6:
                    break
            for r in resp:
                try:
                    for it in r.json().get("itemList") or []:
                        st = it.get("stats") or {}
                        items.append({"title": (it.get("desc") or "video")[:70], "views": _num(st.get("playCount")),
                                      "likes": _num(st.get("diggCount")), "comments": _num(st.get("commentCount")),
                                      "t": it.get("createTime")})
                except Exception:
                    pass
        finally:
            b.close()
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -(int(x["t"] or 0))):
        k = (it["title"], it["t"])
        if k not in seen:
            seen.add(k)
            ago = ""
            if it["t"]:
                d = (time.time() - int(it["t"])) / 86400
                ago = f"{int(d)}d ago" if d >= 1 else f"{int(d * 24)}h ago"
            out.append({"title": it["title"], "views": it["views"], "likes": it["likes"],
                        "comments": it["comments"], "age": ago})
    return out[:8]


def fetch(handles: dict) -> dict:
    res = {}
    browser_jobs = {p: h for p, h in handles.items() if p in ("instagram", "youtube") and h}
    with ThreadPoolExecutor(3) as ex:
        fb = ex.submit(_browser_pages, browser_jobs) if browser_jobs else None
        ft = ex.submit(_tiktok_views, handles["tiktok"]) if handles.get("tiktok") else None
        if handles.get("tiktok"):
            try:
                res["tiktok"] = _tiktok(handles["tiktok"])
                res["tiktok"]["recent"] = ft.result(timeout=60)
            except Exception as e:
                res.setdefault("tiktok", {"error": str(e)[:160]})
        if fb:
            try:
                res.update(fb.result(timeout=90))
            except Exception as e:
                for p in browser_jobs:
                    res[p] = {"error": str(e)[:160]}
    return res


# ── history + report ────────────────────────────────────────────────────────
def _fmt(n) -> str:
    if n is None:
        return "?"
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= d * 10 or (abs(n) >= d and d >= 1e6):
            return f"{n / d:.1f}".rstrip("0").rstrip(".") + s
    return f"{n:,}"


def _baseline(hist: list, plat: str, older_than_s: float):
    now = time.time()
    for snap in reversed(hist):
        if now - snap["t"] >= older_than_s and plat in snap["stats"] and "error" not in snap["stats"][plat]:
            return snap
    return None


def check(platform: str = "all") -> str:
    handles = _load(CFG, {})
    if not any(handles.get(p) for p in PLATFORMS):
        return ("No accounts saved yet. Ask the user for their Instagram, YouTube and TikTok usernames, "
                "then call social_stats(action='set', platform=…, handle=…) for each.")
    want = {p: _clean(h) for p, h in handles.items() if p in PLATFORMS and h and platform in ("all", p, "", None)}
    if not want:
        return f"No {platform} account saved. Ask for the username and save it with action='set'."
    stats = fetch(want)
    with _lock:
        hist = _load(HIST, [])
        prev_all = list(hist)
        ok = {p: {k: v for k, v in s.items() if k != "recent"} for p, s in stats.items() if "error" not in s}
        if ok:
            hist.append({"t": time.time(), "date": datetime.now().isoformat(timespec="minutes"), "stats": ok})
            _save(HIST, hist[-2000:])
    lines = []
    for plat in PLATFORMS:
        if plat not in stats:
            continue
        s = stats[plat]
        name = {"instagram": "Instagram", "youtube": "YouTube", "tiktok": "TikTok"}[plat]
        if "error" in s:
            lines.append(f"{name} @{want[plat]}: couldn't read it ({s['error']})")
            continue
        rec = s.get("recent") or []
        parts = [f"{k} {_fmt(v)}" for k, v in s.items() if v is not None and k != "recent"]
        main = "subscribers" if plat == "youtube" else "followers"
        growth = []
        for label, secs in (("since the last check", 60), ("in 24 h", 20 * 3600), ("in 7 days", 6.5 * 86400)):
            b = _baseline(prev_all, plat, secs)
            if b and s.get(main) is not None and b["stats"][plat].get(main) is not None:
                d = s[main] - b["stats"][plat][main]
                g = f"{'+' if d >= 0 else ''}{_fmt(d)} {main} {label}"
                if g not in growth:
                    growth.append(g)
        lines.append(f"{name} @{want[plat]}: " + ", ".join(parts) + (f" ({'; '.join(growth)})" if growth else ""))
        if rec:
            vs = [r["views"] for r in rec if r.get("views") is not None]
            lines.append(f"  latest {len(rec)} {'videos' if plat != 'instagram' else 'reels'}: "
                         f"{_fmt(sum(vs))} views total, avg {_fmt(sum(vs) // max(1, len(vs)))}")
            for r in rec[:5]:
                extra = "".join(f", {_fmt(r[k])} {k}" for k in ("likes", "comments") if r.get(k) is not None)
                lines.append(f"   - {r['title']}: {_fmt(r['views'])} views{extra}{(' (' + r['age'] + ')') if r.get('age') else ''}")
    note = ("\n(Public numbers. Instagram/YouTube round big counts. Reach, watch time and profile visits "
            "are only in the apps' private analytics.)")
    return "\n".join(lines) + note


def set_handle(platform: str, handle: str) -> str:
    platform = (platform or "").lower().strip()
    if platform in ("insta", "ig"):
        platform = "instagram"
    if platform in ("yt",):
        platform = "youtube"
    if platform not in PLATFORMS:
        return "platform must be instagram, youtube or tiktok"
    h = _clean(handle)
    if not h:
        return "Give me the username."
    with _lock:
        cfg = _load(CFG, {})
        cfg[platform] = h
        _save(CFG, cfg)
    return f"Saved {platform} @{h}. " + check(platform)


def social_stats(args: dict) -> str:
    act = str(args.get("action") or "check").lower()
    multi = {p: args[p] for p in PLATFORMS if args.get(p)}
    if act == "set" or multi:
        if multi:
            with _lock:
                cfg = _load(CFG, {})
                cfg.update({p: _clean(h) for p, h in multi.items()})
                _save(CFG, cfg)
            return "Saved " + ", ".join(f"{p} @{_clean(h)}" for p, h in multi.items()) + ". " + check("all")
        return set_handle(args.get("platform", ""), args.get("handle", ""))
    if act == "accounts":
        cfg = _load(CFG, {})
        return ", ".join(f"{p}: @{cfg[p]}" for p in PLATFORMS if cfg.get(p)) or "No accounts saved."
    return check(str(args.get("platform") or "all").lower())


if __name__ == "__main__":
    import sys
    print(fetch({"tiktok": sys.argv[1], "youtube": sys.argv[2], "instagram": sys.argv[3]}))

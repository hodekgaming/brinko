# video_studio.py — BRINKO's short-video studio (free, local).
#
#   trends(topic)            what's going viral on TikTok for a topic (tag pages read through an off-screen
#                            real Chrome, like social_stats) → stats + an AI style guide (pace, length, hook,
#                            sound, captions, effects), saved to memory/trends/<topic>.json
#   analyze(url_or_path)     downloads a TikTok/Reel/Short (yt-dlp) or takes a local file, Gemini watches it
#                            and explains why it works + how to copy the style
#   voiceover(text)          Kokoro (local, free, ElevenLabs-like) → wav + word timings
#   make_edit(...)           beat-synced edit from the user's own footage + a song: cuts on the beat, zoom
#                            punches, flashes, shake, colour grade per style (anime / stranger things / hype…)
#   make_reel(...)           full talking reel: script (AI, in the trend's style) → Kokoro voice → footage or a
#                            dark animated background → karaoke captions (white, current word orange, like the
#                            user's FormProof reels) → voice + ducked music → 1080x1920 mp4
# Output: ~/Documents/BRINKO/Videos. Everything runs on ffmpeg; no paid APIs.

import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
OUT_DIR = Path.home() / "Documents" / "BRINKO" / "Videos"
TREND_DIR = BASE / "memory" / "trends"
KOKORO_DIR = BASE / "models" / "kokoro"
KOKORO_FALLBACK = Path.home() / "Desktop" / "FormProof ADs" / "reel-voice"
W, H, FPS = 1080, 1920, 30
VID_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
AUD_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

STYLES = {
    # grade, cut on every Nth beat at the start, punch every Nth cut, flash, shake
    "anime":           {"grade": "eq=contrast=1.12:saturation=1.45:gamma=0.97,unsharp=5:5:0.8",
                        "every": 1, "punch": 2, "flash": True, "shake": True},
    "stranger things": {"grade": "eq=contrast=1.18:saturation=0.85:brightness=-0.03,"
                                 "colorbalance=rs=0.12:gs=-0.04:bs=-0.06:rm=0.08,noise=alls=10:allf=t,vignette=PI/4",
                        "every": 2, "punch": 2, "flash": True, "shake": False},
    "hype":            {"grade": "eq=contrast=1.15:saturation=1.25", "every": 1, "punch": 1, "flash": True, "shake": True},
    "cinematic":       {"grade": "eq=contrast=1.08:saturation=0.9,colorbalance=bs=0.06:rh=0.05,vignette=PI/5",
                        "every": 4, "punch": 4, "flash": False, "shake": False},
    "clean":           {"grade": "eq=contrast=1.05:saturation=1.08", "every": 2, "punch": 3, "flash": False, "shake": False},
}


def _style(name: str) -> dict:
    n = (name or "").lower()
    for k in STYLES:
        if k in n or (k == "stranger things" and ("stranger" in n or "80s" in n or "horror" in n)):
            return STYLES[k]
    if any(w in n for w in ("amv", "edit", "jjk", "naruto", "one piece", "manga")):
        return STYLES["anime"]
    return STYLES["hype"]


# ── helpers ─────────────────────────────────────────────────────────────────
def _ff(args: list, timeout: int = 600):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args,
                       capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + (r.stderr or "")[-400:])


def _dur(path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, creationflags=NOWIN)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _out(name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "_")[:40] or "video"
    return OUT_DIR / f"{safe}_{datetime.now():%Y%m%d_%H%M%S}.mp4"


def _resolve(p) -> Path:
    """'Downloads\ST', 'my Videos/edits', 'Desktop/clips' → a real path under the user's home folders."""
    q = Path(os.path.expandvars(os.path.expanduser(str(p).strip().strip('"'))))
    if q.exists() or q.is_absolute():
        return q
    home = Path.home()
    rel = re.sub(r"^(my|the)\s+", "", str(p).strip(), flags=re.I).replace("\\", "/")
    for root in (home, home / "OneDrive", home / "Desktop", home / "Videos", home / "Downloads", home / "Documents"):
        c = root / rel
        if c.exists():
            return c
        first = rel.split("/")[0].lower()           # case-insensitive first part ('downloads/st')
        if root.exists():
            for d in root.iterdir():
                if d.name.lower() == first:
                    c = d / "/".join(rel.split("/")[1:]) if "/" in rel else d
                    if c.exists():
                        return c
    return q


def _expand(paths) -> list[Path]:
    """Files or folders (any mix, ; or , separated) → existing video files."""
    if isinstance(paths, str):
        paths = [p.strip().strip('"') for p in re.split(r"[;\n]|,(?=\s*[A-Za-z]:)", paths) if p.strip()]
    out = []
    for p in paths or []:
        q = _resolve(p)
        if q.is_dir():
            out += sorted(f for f in q.rglob("*") if f.suffix.lower() in VID_EXT)
        elif q.is_file() and q.suffix.lower() in VID_EXT:
            out.append(q)
    return out


def _find_music(music: str | None, folder_hint: list[Path]) -> Path | None:
    if music:
        m = _resolve(music.strip().strip('"'))
        if m.is_file():
            return m
        if m.is_dir():
            c = [f for f in m.iterdir() if f.suffix.lower() in AUD_EXT]
            return c[0] if c else None
    for f in folder_hint:                                  # a song next to the footage
        for g in f.parent.iterdir():
            if g.suffix.lower() in AUD_EXT:
                return g
    return None


def _chat(prompt: str, json_mode=False, smart=True):
    from or_client import client
    if json_mode:
        return client.chat_json(prompt, max_tokens=4000)
    try:
        return client.chat(prompt, smart=smart, max_tokens=4000)
    except TypeError:
        return client.chat(prompt)


# ── TRENDS ──────────────────────────────────────────────────────────────────
def _tiktok_tag_items(tag: str) -> list[dict]:
    from playwright.sync_api import sync_playwright
    items = []
    with sync_playwright() as p:
        b = None
        for ch in ("chrome", "msedge"):
            try:
                b = p.chromium.launch(channel=ch, headless=False, args=[
                    "--window-position=-2400,0", "--window-size=1100,900", "--disable-blink-features=AutomationControlled"])
                break
            except Exception:
                continue
        if not b:
            return []
        try:
            ctx = b.new_context(locale="en-US")
            res = []
            ctx.on("response", lambda r: res.append(r) if "item_list" in r.url else None)
            pg = ctx.new_page()
            pg.goto(f"https://www.tiktok.com/tag/{tag}", wait_until="domcontentloaded", timeout=30000)
            for i in range(18):
                pg.wait_for_timeout(500)
                if i == 8:
                    pg.mouse.wheel(0, 3000)
            for r in res:
                try:
                    for it in r.json().get("itemList") or []:
                        st, v, m, a = it.get("stats") or {}, it.get("video") or {}, it.get("music") or {}, it.get("author") or {}
                        items.append({"views": int(st.get("playCount") or 0), "likes": int(st.get("diggCount") or 0),
                                      "shares": int(st.get("shareCount") or 0), "comments": int(st.get("commentCount") or 0),
                                      "duration": v.get("duration"), "sound": m.get("title", ""), "sound_author": m.get("authorName", ""),
                                      "desc": (it.get("desc") or "")[:160], "author": a.get("uniqueId", ""),
                                      "url": f"https://www.tiktok.com/@{a.get('uniqueId','')}/video/{it.get('id','')}",
                                      "created": it.get("createTime")})
                except Exception:
                    pass
        finally:
            b.close()
    seen, out = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"]); out.append(it)
    return out


def trends(topic: str, platform: str = "tiktok") -> str:
    topic = (topic or "").strip() or "viral"
    base = re.sub(r"[^a-z0-9]", "", topic.lower())
    tags = [base] if base.endswith("edit") else [base + "edit", base]
    items = []
    for t in tags:
        items += _tiktok_tag_items(t)
        if len(items) >= 40:
            break
    if not items:
        return f"Couldn't read TikTok trends for '{topic}' right now (TikTok may be blocking; try again in a minute)."
    now = time.time()
    recent = [i for i in items if i.get("created") and now - int(i["created"]) < 120 * 86400] or items
    top = sorted(recent, key=lambda x: -x["views"])[:15]
    sounds = {}
    for i in recent:
        if i["sound"]:
            sounds[i["sound"]] = sounds.get(i["sound"], 0) + 1
    durs = [i["duration"] for i in top if i.get("duration")]
    facts = {"topic": topic, "videos_read": len(items), "top": top,
             "median_length_s": sorted(durs)[len(durs) // 2] if durs else None,
             "popular_sounds": sorted(sounds.items(), key=lambda x: -x[1])[:6]}
    guide = {}
    try:
        guide = _chat(
            "You are a short-form video strategist. From these REAL TikTok stats for the topic "
            f"'{topic}', write a style guide for making a video that fits what is going viral. JSON keys: "
            "summary (2 sentences), length_s (int), cuts_per_second (float), hook (what the first 1-2 s do), "
            "sound (what kind of audio / which trending sound), captions (style), effects (list), "
            "edit_style (one of: anime, stranger things, hype, cinematic, clean), script_tips (list), "
            "ideas (3 concrete video ideas). Data:\n" + json.dumps(facts)[:9000], json_mode=True) or {}
    except Exception as e:
        guide = {"summary": f"(analysis failed: {e})"}
    TREND_DIR.mkdir(parents=True, exist_ok=True)
    (TREND_DIR / f"{base}.json").write_text(json.dumps({"facts": facts, "guide": guide, "t": now}, indent=1), encoding="utf-8")
    lines = [f"TikTok trend check for '{topic}' ({len(items)} videos read). Top right now:"]
    for i in top[:5]:
        lines.append(f"- {i['views']:,} views, {i['duration']}s, sound '{i['sound'][:40]}': {i['desc'][:80]}")
    if facts["popular_sounds"]:
        lines.append("Most used sounds: " + "; ".join(f"{s} ({n}x)" for s, n in facts["popular_sounds"][:3]))
    if guide:
        lines.append("Style guide: " + json.dumps(guide, ensure_ascii=False)[:1500])
    return "\n".join(lines)


def _guide_for(topic: str) -> dict:
    base = re.sub(r"[^a-z0-9]", "", (topic or "").lower())
    for f in (TREND_DIR.glob("*.json") if TREND_DIR.exists() else []):
        if base and (base in f.stem or f.stem in base):
            try:
                return json.loads(f.read_text(encoding="utf-8")).get("guide") or {}
            except Exception:
                pass
    return {}


# ── ANALYZE a video ─────────────────────────────────────────────────────────
def _tiktok_download(url: str, dest: Path) -> Path:
    """TikTok refuses plain downloaders; open the video page in an off-screen real Chrome and save the
    video stream it loads."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = None
        for ch in ("chrome", "msedge"):
            try:
                b = p.chromium.launch(channel=ch, headless=False, args=[
                    "--window-position=-2400,0", "--window-size=900,1000", "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
                break
            except Exception:
                continue
        if not b:
            raise RuntimeError("needs Chrome or Edge installed")
        try:
            ctx = b.new_context(locale="en-US")
            vids = []
            ctx.on("response", lambda r: vids.append(r.url) if ("video/" in (r.headers.get("content-type") or "")
                                                                 or "/video/tos/" in r.url) else None)
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            for _ in range(24):
                pg.wait_for_timeout(500)
                if vids:
                    break
            if not vids:
                raise RuntimeError("TikTok didn't load the video")
            r = ctx.request.get(vids[0], headers={"Referer": "https://www.tiktok.com/", "Range": "bytes=0-"}, timeout=60000)
            body = r.body()
            if len(body) < 50000:
                raise RuntimeError("TikTok sent an empty video")
            dest.write_bytes(body)
            return dest
        finally:
            b.close()


def _download(url: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="brinko_dl_"))
    if "tiktok.com" in url:
        try:
            return _tiktok_download(url, tmp / "v.mp4")
        except Exception as e:
            print(f"[Studio] tiktok browser download: {e}")
    r = subprocess.run([os.sys.executable, "-m", "yt_dlp", "-q", "--no-playlist", "-f", "mp4/best[ext=mp4]/best",
                        "--max-filesize", "150M", "-o", str(tmp / "v.%(ext)s"), url],
                       capture_output=True, text=True, timeout=180, creationflags=NOWIN)
    files = list(tmp.glob("v.*"))
    if not files:
        raise RuntimeError("download failed: " + (r.stderr or "")[-200:])
    return files[0]


def analyze(source: str, question: str = "") -> str:
    from google import genai
    keys = json.loads((BASE / "config" / "api_keys.json").read_text(encoding="utf-8"))
    path = Path(source) if Path(source).is_file() else _download(source)
    client = genai.Client(api_key=keys["gemini_api_key"])
    f = client.files.upload(file=str(path))
    for _ in range(60):
        f = client.files.get(name=f.name)
        if str(getattr(f.state, "name", f.state)) == "ACTIVE":
            break
        if "FAIL" in str(f.state):
            raise RuntimeError("Gemini couldn't process the video")
        time.sleep(2)
    prompt = ("You are a viral short-form video editor. Watch this video closely and explain: 1) the hook in the "
              "first 2 seconds, 2) cut pace (roughly cuts per second) and how cuts line up with the beat, 3) effects "
              "and transitions (zooms, flashes, shakes, speed ramps, colour), 4) text/captions, 5) audio (voice? "
              "trending sound?), 6) why it likely went viral, 7) a recipe to make one in the same style. "
              "Be concrete and short. " + (question or ""))
    for model in ("gemini-3.5-flash", "gemini-3.1-flash-lite"):
        try:
            r = client.models.generate_content(model=model, contents=[f, prompt])
            return (r.text or "").strip()
        except Exception as e:
            err = e
    raise RuntimeError(f"analysis failed: {err}")


# ── VOICE (Kokoro, local) ───────────────────────────────────────────────────
VOICES = {"female": "af_heart", "male": "am_michael", "british": "bf_emma", "deep": "am_onyx",
          "energetic": "af_nicole", "calm": "af_sky"}
_kokoro = None


KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def _ensure_kokoro():
    """First use on a new PC: fetch the free Kokoro model (~115 MB) into models/kokoro."""
    if (KOKORO_DIR / "kokoro-v1.0.int8.onnx").exists() or (KOKORO_FALLBACK / "kokoro-v1.0.int8.onnx").exists():
        return
    import requests
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for f in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        tmp = KOKORO_DIR / (f + ".part")
        with requests.get(KOKORO_URL + f, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        tmp.replace(KOKORO_DIR / f)


def _kk():
    global _kokoro
    if _kokoro is None:
        _ensure_kokoro()
        from kokoro_onnx import Kokoro
        d = KOKORO_DIR if (KOKORO_DIR / "kokoro-v1.0.int8.onnx").exists() else KOKORO_FALLBACK
        _kokoro = Kokoro(str(d / "kokoro-v1.0.int8.onnx"), str(d / "voices-v1.0.bin"))
    return _kokoro


def voiceover(text: str, voice: str = "male", out: Path | None = None, speed: float = 1.05):
    """→ (wav_path, duration_s, word_timings[{text,start,end}])"""
    import soundfile as sf
    k = _kk()
    v = VOICES.get((voice or "").lower(), voice or "am_michael")
    if v not in k.get_voices():
        v = "am_michael"
    lines = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    chunks, words, t0, sr = [], [], 0.0, 24000
    for line in lines:
        audio, sr = k.create(line, voice=v, speed=speed, lang="en-us", trim=True)
        dur = len(audio) / sr
        ws = re.findall(r"[\w'’%$€-]+[.!?,]?", line)
        wts = [max(len(re.sub(r'\W', '', w)), 2) + 1 for w in ws]
        tot, t = sum(wts) or 1, 0.0
        for w, wt in zip(ws, wts):
            d = dur * wt / tot
            words.append({"text": w, "start": t0 + t, "end": t0 + t + d})
            t += d
        gap = 0.16
        chunks += [audio, np.zeros(int(gap * sr), dtype=audio.dtype)]
        t0 += dur + gap
    full = np.concatenate(chunks) if chunks else np.zeros(sr, dtype=np.float32)
    out = out or Path(tempfile.mkdtemp(prefix="brinko_vo_")) / "voice.wav"
    sf.write(str(out), full, sr)
    return out, len(full) / sr, words


# ── BEATS ───────────────────────────────────────────────────────────────────
def beats(audio_path, start: float = 0.0, length: float = 60.0) -> tuple[list[float], float]:
    """Beat times (s, relative to `start`) + tempo, from spectral flux + tempo-aware peak picking."""
    sr, hop, n = 22050, 512, 2048
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(length), "-i", str(audio_path),
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, creationflags=NOWIN)
    x = np.frombuffer(r.stdout, dtype=np.float32)
    if len(x) < sr:
        return [], 120.0
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    spec = np.log1p(np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(np.diff(spec, axis=0), 0).sum(axis=1)
    flux = (flux - flux.mean()) / (flux.std() + 1e-9)
    fps = sr / hop
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    lags = np.arange(len(ac))
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 70)
    lag = lo + int(np.argmax(ac[lo:hi])) if hi < len(ac) else int(fps * 0.5)
    bpm = 60 * fps / lag
    # dynamic programming beat tracker (Ellis-style): reward onset strength, penalise off-tempo gaps
    score = flux.copy()
    back = np.full(len(flux), -1)
    for i in range(len(flux)):
        a, b = i - int(lag * 2), i - int(lag * 0.5)
        if b <= 0:
            continue
        a = max(a, 0)
        prev = np.arange(a, b)
        pen = -((np.log((i - prev) / lag)) ** 2) * 6
        j = int(np.argmax(score[prev] + pen))
        score[i] = flux[i] + score[prev[j]] + pen[j]
        back[i] = prev[j]
    i = int(np.argmax(score[-int(lag * 2):])) + len(score) - int(lag * 2)
    bt = []
    while i >= 0:
        bt.append(i)
        i = back[i]
    times = sorted(t * hop / sr for t in bt)
    return [t for t in times if t <= length], bpm


def _strong(audio_path, start, length, times: list[float]) -> list[float]:
    """Loudness at each beat (0-1) → where to put punches/flashes."""
    sr = 11025
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(length), "-i", str(audio_path),
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, creationflags=NOWIN)
    x = np.abs(np.frombuffer(r.stdout, dtype=np.float32))
    if not len(x):
        return [0.5] * len(times)
    vals = [float(x[int(t * sr):int(t * sr) + int(0.12 * sr)].mean()) if int(t * sr) < len(x) else 0 for t in times]
    m = max(vals) or 1
    return [v / m for v in vals]


# ── rendering pieces ────────────────────────────────────────────────────────
def _cover():
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}"


def _segment(src: Path, ss: float, dur: float, out: Path, grade: str, punch=False, flash=False, shake=False):
    vf = [_cover(), grade]
    if punch:          # fast zoom-in that settles
        vf.append(f"scale=w='iw*(1+0.14*exp(-t*7))':h='ih*(1+0.14*exp(-t*7))':eval=frame,crop={W}:{H}")
    if shake:
        vf.append(f"pad={W+40}:{H+40}:20:20:black,crop={W}:{H}:x='20+14*sin(t*73)*exp(-t*9)':y='20+14*cos(t*61)*exp(-t*9)'")
    if flash:
        vf.append("eq=brightness='0.45*exp(-t*22)':eval=frame")
    vf.append("format=yuv420p")
    _ff(["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-an", "-vf", ",".join(vf),
         "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", str(out)])


def _concat(parts: list[Path], out: Path):
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    _ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])


def _plan_cuts(clips: list[Path], cut_times: list[float]) -> list[tuple[Path, float, float]]:
    """(clip, start, duration) for each cut; spreads usage over all clips and skips the first/last 0.3 s."""
    durs = {c: _dur(c) for c in clips}
    used = {c: [] for c in clips}
    plan, order = [], []
    for i in range(len(cut_times) - 1):
        d = cut_times[i + 1] - cut_times[i]
        if not order:
            order = random.sample(clips, len(clips))
        c = order.pop()
        span = max(0.1, durs[c] - d - 0.6)
        for _ in range(8):
            s = 0.3 + random.random() * span
            if all(abs(s - u) > d for u in used[c]):
                break
        used[c].append(s)
        plan.append((c, s, d))
    return plan


# ── EDIT from the user's footage ────────────────────────────────────────────
def make_edit(footage, music: str | None = None, style: str = "hype", length: float = 20.0,
              music_start: float | None = None, title: str = "", progress=None) -> Path:
    clips = _expand(footage)
    if not clips:
        raise RuntimeError("No video files found in the footage you gave me (folder or file paths).")
    song = _find_music(music, clips)
    st = _style(style)
    length = max(6.0, min(float(length or 20), 90.0))
    tmp = Path(tempfile.mkdtemp(prefix="brinko_edit_"))
    if song:
        total = _dur(song)
        if music_start is None:                     # loudest stretch (likely the drop/chorus)
            music_start = _best_window(song, total, length)
        bt, bpm = beats(song, music_start, length)
        strength = _strong(song, music_start, length, bt)
    else:
        bpm, music_start = 120.0, 0.0
        bt = [i * 0.5 for i in range(int(length / 0.5) + 1)]
        strength = [0.6] * len(bt)
    every = st["every"]
    cuts = [0.0]
    for i, t in enumerate(bt):
        intro = t < length * 0.18 and every < 2          # slower cuts in the intro, faster after
        step = every * (2 if intro else 1)
        if i % step == 0 and t - cuts[-1] > 0.22:
            cuts.append(t)
    if cuts[-1] < length:
        cuts.append(length)
    plan = _plan_cuts(clips, cuts)
    parts = []
    for i, (c, s, d) in enumerate(plan):
        loud = strength[min(i * every, len(strength) - 1)] if strength else 0.5
        p = tmp / f"s{i:03d}.mp4"
        _segment(c, s, d, p, st["grade"], punch=(i % st["punch"] == 0 and loud > 0.45),
                 flash=st["flash"] and loud > 0.8, shake=st["shake"] and loud > 0.7 and i % 3 == 0)
        parts.append(p)
        if progress:
            progress(f"cut {i + 1}/{len(plan)}")
    silent = tmp / "video.mp4"
    _concat(parts, silent)
    out = _out(title or f"{style} edit")
    vf = []
    if title:
        vf.append(_drawtext(title, y="h*0.12", size=78, until=2.2))
    args = ["-i", str(silent)]
    if song:
        args += ["-ss", f"{music_start:.3f}", "-t", f"{length:.3f}", "-i", str(song),
                 "-filter_complex", f"[1:a]afade=t=out:st={max(0, length - 1.2):.2f}:d=1.2[a]",
                 "-map", "0:v", "-map", "[a]"]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-t", f"{length:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    _ff(args)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def _best_window(song: Path, total: float, length: float) -> float:
    if total <= length + 2:
        return 0.0
    sr = 4000
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(song), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True, creationflags=NOWIN)
    x = np.abs(np.frombuffer(r.stdout, dtype=np.float32))
    sec = x[: len(x) // sr * sr].reshape(-1, sr).mean(axis=1)
    win = int(length)
    if len(sec) <= win:
        return 0.0
    c = np.convolve(sec, np.ones(win), "valid")
    c[: int(len(c) * 0.15)] *= 0.85                    # avoid the very start
    return float(max(0, int(np.argmax(c)) - 0.5))


# ── captions (ASS) + drawtext ───────────────────────────────────────────────
def _font():
    for f in ("C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if Path(f).exists():
            return f
    return ""


def _drawtext(text, y="h*0.12", size=72, until=None):
    t = text.upper().replace("\\", "").replace(":", "\\:").replace("'", "\u2019")
    en = f":enable='lt(t,{until})'" if until else ""
    ff = _font().replace(":", chr(92) + ":")
    return (f"drawtext=fontfile='{ff}':text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=6:bordercolor=black@0.85:x=(w-tw)/2:y={y}{en}")


def _ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _captions_ass(words: list[dict], path: Path, accent="&H001F5AFF&", per_page=3):
    """Karaoke pages at the top third: white words, the spoken one in the accent colour (BGR) and slightly bigger."""
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            "Style: Cap,Arial Black,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,7,3,8,70,70,330,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    pages, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= per_page or re.search(r"[.!?]$", w["text"]):
            pages.append(cur); cur = []
    if cur:
        pages.append(cur)
    for pi, pg in enumerate(pages):
        end_page = pages[pi + 1][0]["start"] if pi + 1 < len(pages) else pg[-1]["end"] + 0.4
        for wi, w in enumerate(pg):
            s = w["start"]
            e = pg[wi + 1]["start"] if wi + 1 < len(pg) else end_page
            txt = []
            for wj, x in enumerate(pg):
                word = re.sub(r"[{}\\]", "", x["text"]).upper()
                txt.append(("{\\c" + accent + "\\fscx112\\fscy112}" + word + "{\\c&H00FFFFFF&\\fscx100\\fscy100}")
                           if wj == wi else word)
            ev.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{' '.join(txt)}")
    path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ── full REEL with voice ────────────────────────────────────────────────────
def write_script(topic: str, seconds: int = 25, style_hint: str = "") -> str:
    guide = _guide_for(topic)
    try:
        from memory.memory_manager import load_memory
        mem = json.dumps(load_memory().get("projects", {}))[:1200]
    except Exception:
        mem = ""
    words = int(seconds * 2.6)
    p = (f"Write the voiceover for a {seconds}-second vertical short (TikTok/Reels) about: {topic}. "
         f"~{words} words. First line = a scroll-stopping hook (max 9 words). Short punchy sentences, spoken "
         f"English, no hashtags, no emojis, no stage directions, end with a clear call to action. "
         f"{style_hint} " + (f"Trend style guide to follow: {json.dumps(guide)[:1500]} " if guide else "")
         + (f"Context about the creator's projects: {mem}" if mem else "") + " Return ONLY the voiceover text.")
    return re.sub(r"^\W*(voiceover|script)\s*:\s*", "", (_chat(p) or "").strip(), flags=re.I).strip().strip('"')


def _bg_video(duration: float, out: Path):
    """Dark animated background like the user's reels: near-black with a slow drifting red/orange glow + grain.
    The glow is computed at 1/4 size (it's blurry anyway) and scaled up — ~10x faster than full-res geq."""
    w, h = W // 4, H // 4
    g = f"exp(-((X-{w//2}-35*sin(T*0.6))^2+(Y-{int(h*0.57)}-45*cos(T*0.45))^2)/5600)"
    _ff(["-f", "lavfi", "-i", f"nullsrc=s={w}x{h}:r={FPS}:d={duration:.2f}",
         "-vf", f"geq=r='16+95*{g}':g='16+26*{g}':b='20+14*{g}',scale={W}:{H}:flags=bicubic,"
                "noise=alls=5:allf=t,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", str(out)], timeout=900)


def make_reel(topic: str = "", script: str = "", footage=None, music: str | None = None, voice: str = "male",
              seconds: int = 25, title: str = "", progress=None) -> tuple[Path, str]:
    script = (script or "").strip() or write_script(topic, seconds)
    if not script:
        raise RuntimeError("couldn't write a script")
    tmp = Path(tempfile.mkdtemp(prefix="brinko_reel_"))
    if progress:
        progress("voice")
    vo, vdur, words = voiceover(script, voice, tmp / "voice.wav")
    length = vdur + 0.6
    clips = _expand(footage) if footage else []
    song = _find_music(music, clips) if (music or clips) else None
    if progress:
        progress("visuals")
    if clips:
        if song:
            ms = _best_window(song, _dur(song), length)
            bt, _ = beats(song, ms, length)
            cuts = [0.0]
            for t in bt:
                if t - cuts[-1] >= 1.1:                  # talking reels: calmer cuts than pure edits
                    cuts.append(t)
        else:
            ms = 0.0
            cuts = [0.0] + [w["start"] for w in words if re.search(r"[.!?]$", w["text"])][:-1]
            cuts = [c for i, c in enumerate(cuts) if i == 0 or c - cuts[i - 1] > 1.0]
        if cuts[-1] < length:
            cuts.append(length)
        parts = []
        for i, (c, s, d) in enumerate(_plan_cuts(clips, cuts)):
            p = tmp / f"s{i:03d}.mp4"
            _segment(c, s, d, p, "eq=contrast=1.06:saturation=1.08,eq=brightness=-0.06", punch=(i % 3 == 0))
            parts.append(p)
        base = tmp / "base.mp4"
        _concat(parts, base)
    else:
        ms = 0.0
        base = tmp / "base.mp4"
        _bg_video(length, base)
    ass = tmp / "cap.ass"
    _captions_ass(words, ass)
    out = _out(title or topic or "reel")
    if progress:
        progress("render")
    sub = "subtitles='" + ass.as_posix().replace(":", "\\:") + "'"
    vf = sub + ("," + _drawtext(title, y="h*0.07", size=56) if title else "")
    args = ["-i", str(base), "-i", str(vo)]
    if song:
        args += ["-ss", f"{ms:.3f}", "-t", f"{length:.3f}", "-i", str(song), "-filter_complex",
                 f"[2:a]volume=0.55,afade=t=out:st={max(0, length - 1.2):.2f}:d=1.2[m];"
                 f"[m][1:a]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=350[duck];"
                 f"[duck][1:a]amix=inputs=2:duration=first:normalize=0[a];[0:v]{vf}[v]",
                 "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a"]
    args += ["-t", f"{length:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    _ff(args, timeout=1200)
    shutil.rmtree(tmp, ignore_errors=True)
    return out, script


# ── tool entry ──────────────────────────────────────────────────────────────
def video_studio(args: dict, player=None) -> str:
    a = str(args.get("action") or "").lower()
    log = (lambda m: player.write_log(f"SYS: 🎬 {m}")) if player else None
    try:
        if a == "trends":
            return trends(args.get("topic", ""))
        if a == "analyze":
            return analyze(args.get("source") or args.get("url") or "", args.get("topic", ""))
        if a == "script":
            return write_script(args.get("topic", ""), int(args.get("seconds") or 25))
        if a == "voiceover":
            p, d, _ = voiceover(args.get("script") or args.get("topic", ""), args.get("voice") or "male",
                                _out("voiceover").with_suffix(".wav"))
            return f"Voiceover saved ({d:.1f}s): {p}"
        if a == "edit":
            out = make_edit(args.get("footage"), args.get("music"), args.get("style") or args.get("topic") or "hype",
                            float(args.get("seconds") or 20), title=args.get("title", ""), progress=log)
        elif a in ("reel", "video", "full"):
            out, script = make_reel(args.get("topic", ""), args.get("script", ""), args.get("footage"),
                                    args.get("music"), args.get("voice") or "male", int(args.get("seconds") or 25),
                                    args.get("title", ""), progress=log)
        else:
            return "action must be trends | analyze | script | voiceover | edit | reel"
        try:
            os.startfile(str(out))
        except Exception:
            pass
        return f"Done: {out.name} ({_dur(out):.1f}s, 1080x1920) saved in {out.parent} and opened."
    except Exception as e:
        return f"Video studio error: {str(e)[:300]}"

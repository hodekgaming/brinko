"""Characters for the camera portal (fx/camera.html).

Each character is ONE wide picture: the character from the waist up, facing the
camera, in its own scene.  The portal page lines the character's face up with
the user's face, so whatever part of the body the hand-frame covers shows the
same part of the character (like the helmet video).

Built-in characters ship in fx/characters/.  New ones are made on request from
just the NAME (no camera image ever leaves the PC):
  1. FLUX.1-schnell on free Hugging Face Spaces (2048x1152, sharp, ~15 s).  Without a
     token the free GPU quota is tiny (a couple of images, then it refills slowly);
     a free Hugging Face token in config/api_keys.json as "huggingface_token" raises it a lot.
  2. Fallback: Pollinations' free endpoint (no key, 1 image / 45 s, only a small model,
     ~1015x580 with a watermark we crop off) - clearly worse looking.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "fx" / "characters"
INDEX = DIR / "characters.json"

PROMPT = ("{who}, medium shot from the waist up, facing the camera straight on, centered, the whole head and both "
          "shoulders visible with space above the head, {scene}, cinematic lighting, highly detailed, sharp focus, "
          "IMAX movie still")
FLUX_SPACES = [  # (space, endpoint, kwargs(prompt, seed))
    ("black-forest-labs/FLUX.1-schnell", "/infer",
     lambda p, s: dict(prompt=p, seed=s, randomize_seed=False, width=2048, height=1152, num_inference_steps=4)),
    ("KingNish/Realtime-FLUX", "/generate_image_1",
     lambda p, s: dict(prompt=p, seed=s, width=2048, height=1152, randomize_seed=False, num_inference_steps=4)),
]
# default face position for pictures made with PROMPT (fractions of the picture)
DEFAULT_FACE = {"x": 0.5, "y": 0.3, "w": 0.13}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_last_call = 0.0


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())[:40] or "character"


def load() -> list[dict]:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list[dict]):
    INDEX.write_text(json.dumps(items, indent=2), encoding="utf-8")


def find(name: str) -> dict | None:
    s = _slug(name)
    items = load()
    for c in items:                                   # exact, then partial ("widow" -> Black Widow)
        if c["slug"] == s or _slug(c["name"]) == s:
            return c
    for c in items:
        if s and (s in c["slug"] or c["slug"] in s):
            return c
    return None


def _scene_for(name: str) -> str:
    """Ask the fast model for a short look + matching scene; fall back to a generic one."""
    try:
        from or_client import client
        d = client.chat_json(
            f"Describe '{name}' for an image prompt as JSON {{\"who\": ..., \"scene\": ...}}. "
            "who = the character's look in one line (costume, colours, face/helmet) starting with the name; "
            "scene = where they stand, one short line. No real actor names.", max_tokens=600)
        return str(d.get("who") or name)[:300], str(d.get("scene") or "in a cinematic environment")[:200]
    except Exception:
        return name, "in a cinematic environment that fits the character"


def _fetch(prompt: str, seed: int) -> bytes:
    global _last_call
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
           + f"?width=1344&height=768&nologo=true&model=flux&seed={seed}")
    for attempt in range(4):
        wait = 46 - (time.time() - _last_call)      # free tier: 1 image / 45 s
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "brinko-local"})
            return urllib.request.urlopen(req, timeout=150).read()
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            m = re.search(r"Retry after (\d+)s", body)
            if e.code in (402, 429) and attempt < 3:
                time.sleep(int(m.group(1)) + 1 if m else 20)
                continue
            raise RuntimeError(f"image service said {e.code}")
    raise RuntimeError("image service busy")


def _hf_token() -> str | None:
    try:
        keys = json.loads((DIR.parent.parent / "config" / "api_keys.json").read_text(encoding="utf-8"))
        return (keys.get("huggingface_token") or "").strip() or None
    except Exception:
        return None


def _fetch_flux(prompt: str, seed: int) -> bytes:
    """FLUX on a free Hugging Face Space -> JPEG bytes (raises if every space refuses, e.g. quota)."""
    from gradio_client import Client                       # pip install gradio_client
    from PIL import Image
    errors = []
    for space, endpoint, kwargs in FLUX_SPACES:
        try:
            tok = _hf_token()
            client = Client(space, token=tok, verbose=False) if tok else Client(space, verbose=False)
            r = client.predict(**kwargs(prompt, seed), api_name=endpoint)
            path = r[0] if isinstance(r, (list, tuple)) else r
            buf = io.BytesIO()
            Image.open(str(path)).convert("RGB").save(buf, "JPEG", quality=92)
            return buf.getvalue()
        except Exception as e:
            errors.append(f"{space}: {str(e)[:120]}")
    raise RuntimeError("; ".join(errors))


def _crop_watermark(data: bytes) -> bytes:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        im = im.crop((0, 0, w, int(h * 0.93)))      # watermark sits in the bottom 6%
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return data


def _face_box(path: Path) -> dict:
    """Where the character's face is (fractions of the picture), found by the vision model.
    Same box the live face tracker draws on the user: hairline/forehead to chin, cheek to cheek
    (for helmets and masks: the part that covers the face)."""
    try:
        from or_client import client
        out = client.vision_from_file(
            "Find the main character's FACE in this picture (forehead to chin, cheek to cheek; for a helmet or "
            "mask, the part covering the face). Reply with JSON only: {\"left\": a, \"top\": b, \"right\": c, "
            "\"bottom\": d} as fractions 0-1 of the picture width/height.", str(path),
            system="You locate things in images precisely. Reply with JSON only.", max_tokens=800)
        d = json.loads(re.search(r"\{.*?\}", out or "", re.S).group(0))
        l, t, r, b = (float(d[k]) for k in ("left", "top", "right", "bottom"))
        if max(l, t, r, b) > 1.5:                      # Gemini likes its native 0-1000 box scale
            l, t, r, b = l / 1000, t / 1000, r / 1000, b / 1000
        w = r - l
        if 0.04 < w < 0.6 and 0 < t < b < 1:
            return {"x": round((l + r) / 2, 3), "y": round((t + b) / 2, 3), "w": round(w, 3)}
    except Exception as e:
        print(f"[characters] face box: {e}")
    return dict(DEFAULT_FACE)


def _make(job: str, name: str):
    j = _jobs[job]
    try:
        j["stage"] = "imagining"
        who, scene = _scene_for(name)
        j["stage"] = "drawing"
        prompt, seed = PROMPT.format(who=who, scene=scene), sum(map(ord, name)) % 997
        try:
            img = _fetch_flux(prompt, seed)
        except Exception as e:                              # no GPU quota left: small free model instead
            print(f"[characters] FLUX unavailable ({str(e)[:160]}), using Pollinations")
            j["stage"] = "drawing (backup model)"
            img = _crop_watermark(_fetch(prompt, seed))
        slug = _slug(name)
        (DIR / f"{slug}.jpg").write_bytes(img)
        j["stage"] = "fitting"
        entry = {"slug": slug, "name": name.strip().title()[:30], "file": f"{slug}.jpg",
                 "face": _face_box(DIR / f"{slug}.jpg"), "custom": True}
        with _lock:
            items = [c for c in load() if c["slug"] != slug] + [entry]
            _save(items)
        j.update(done=True, ok=True, character=entry)
    except Exception as e:
        j.update(done=True, ok=False, error=str(e)[:200])


_upgrade = {"running": False, "last": 0.0}


def upgrade_async():
    """Characters marked "upgrade" (shipped as a quick low-res picture) get re-drawn with FLUX in the
    background, one per try, at most every 10 minutes - the free GPU quota refills slowly."""
    if _upgrade["running"] or time.time() - _upgrade["last"] < 600:
        return
    todo = [c for c in load() if c.get("upgrade") and c.get("prompt")]
    if not todo:
        return
    _upgrade.update(running=True, last=time.time())

    def run():
        try:
            c = todo[0]
            img = _fetch_flux(c["prompt"], int(c.get("seed", 11)))
            tmp = DIR / f"{c['slug']}.new.jpg"
            tmp.write_bytes(img)
            face = _face_box(tmp)
            tmp.replace(DIR / c["file"])
            with _lock:
                items = load()
                for it in items:
                    if it["slug"] == c["slug"]:
                        it["face"] = face
                        it.pop("upgrade", None)
                _save(items)
            print(f"[characters] {c['name']} upgraded to FLUX")
        except Exception as e:
            print(f"[characters] upgrade later ({str(e)[:120]})")
        finally:
            _upgrade["running"] = False

    threading.Thread(target=run, daemon=True).start()


def add(name: str) -> dict:
    name = re.sub(r"\s+", " ", (name or "")).strip()[:40]
    if not name:
        return {"ok": False, "error": "no name"}
    existing = find(name)
    if existing:
        return {"ok": True, "done": True, "character": existing}
    job = uuid.uuid4().hex[:10]
    _jobs[job] = {"done": False, "stage": "starting", "name": name}
    threading.Thread(target=_make, args=(job, name), daemon=True).start()
    return {"ok": True, "done": False, "job": job}


def status(job: str) -> dict:
    j = _jobs.get(job)
    return dict(j) if j else {"done": True, "ok": False, "error": "unknown job"}


def remove(slug: str) -> bool:
    with _lock:
        items = load()
        keep = [c for c in items if not (c["slug"] == slug and c.get("custom"))]
        if len(keep) == len(items):
            return False
        _save(keep)
    try:
        (DIR / f"{slug}.jpg").unlink()
    except OSError:
        pass
    return True

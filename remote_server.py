# remote_server.py
"""
BRINKO phone link — serves the BRINKO phone app and its API on port 8765.

  • Phone app (installable web app, Android Chrome): /  (files in phone_app/)
  • API: /api/ask (talk to BRINKO), /api/events (job results), /api/ping
  • Optional inbound phone calls via Twilio: /twilio/voice, /twilio/action

Reaching it from anywhere: install Tailscale on PC + phone (same account),
then run once on the PC:   tailscale serve --bg 8765
The phone app is then at https://<pc-name>.<tailnet>.ts.net — HTTPS, so the
microphone works, and only your own devices can reach it.

Security: every API call needs the pairing token (config/remote_token.json),
which the phone gets by scanning the QR code in BRINKO's Settings.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from phone_brain import BRAIN

BASE_DIR   = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
PHONE_DIR  = BASE_DIR / "phone_app"
TOKEN_PATH = CONFIG_DIR / "remote_token.json"
KEYS_PATH  = CONFIG_DIR / "api_keys.json"
PORT       = 8765
FX_DIR     = BASE_DIR / "fx"
ACTIVE_PORT = PORT          # the port the server really runs on (tests use another)


def fx_url(page: str, query: str = "") -> str:
    """Local URL of an FX page (screensaver, camera, docbuilder)."""
    return f"http://localhost:{ACTIVE_PORT}/fx/{page}" + (f"?{query}" if query else "")


# ── wiring from main.py ──────────────────────────────────────────────────────
def register_brinko(brinko, tool_declarations: list[dict], job_summary=None):
    """Called by BrinkoLive once it exists, so phone requests can use PC tools."""
    BRAIN.attach(brinko, tool_declarations, job_summary)


# ── pairing ──────────────────────────────────────────────────────────────────
def _load_or_create_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        try:
            tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("token", "")
            if tok:
                return tok
        except Exception:
            pass
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(json.dumps({"token": token}, indent=2), encoding="utf-8")
    return token


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def tailscale_info() -> dict:
    """{'installed': bool, 'dns': 'pc.tailnet.ts.net' or '', 'serving': bool}"""
    info = {"installed": False, "dns": "", "serving": False}
    exe = None
    for cand in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            r = subprocess.run([cand, "status", "--json"], capture_output=True, text=True, timeout=6,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and r.stdout.strip().startswith("{"):
                exe = cand
                data = json.loads(r.stdout)
                info["installed"] = True
                info["dns"] = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
                break
        except Exception:
            continue
    if exe and info["dns"]:
        try:
            r = subprocess.run([exe, "serve", "status"], capture_output=True, text=True, timeout=6,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            info["serving"] = str(PORT) in (r.stdout or "")
        except Exception:
            pass
    info["exe"] = exe or ""
    return info


def pairing_info() -> dict:
    """What the 'Connect phone' dialog shows: the best URL (with token) + status."""
    token = _load_or_create_token()
    ts = tailscale_info()
    if ts["dns"] and ts["serving"]:
        url, mode = f"https://{ts['dns']}/?pair={token}", "anywhere"
    else:
        url, mode = f"http://{_local_ip()}:{PORT}/?pair={token}", "home"
    return {"url": url, "mode": mode, "tailscale": ts, "token": token}


def enable_tailscale_serve(on_approval_url=None, timeout: float = 300.0) -> tuple[bool, str]:
    """Runs `tailscale serve --bg 8765`. MUST be called off the GUI thread.

    The first time, Tailscale prints "Serve is not enabled on your tailnet.
    To enable, visit: <url>" and then WAITS until the user approves it in the
    browser. We stream its output, hand that URL to `on_approval_url` (the UI
    opens it), and keep waiting until it finishes or `timeout` passes."""
    import re
    import time as _t
    ts = tailscale_info()
    if not ts["installed"]:
        return False, "Tailscale isn't installed (or not signed in) on this PC."
    try:
        proc = subprocess.Popen([ts["exe"], "serve", "--bg", str(PORT)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return False, str(e)

    out_lines: list[str] = []
    sent_url = [False]

    def _reader():
        for line in proc.stdout:
            out_lines.append(line.rstrip())
            m = re.search(r"https://login\.tailscale\.com/\S+", line)
            if m and not sent_url[0]:
                sent_url[0] = True
                if on_approval_url:
                    try:
                        on_approval_url(m.group(0))
                    except Exception:
                        pass
    threading.Thread(target=_reader, daemon=True).start()

    deadline = _t.time() + timeout
    while proc.poll() is None and _t.time() < deadline:
        _t.sleep(0.3)
    if proc.poll() is None:
        proc.kill()
        return False, "Timed out waiting for the approval in the browser."
    _t.sleep(0.3)
    ok = tailscale_info()["serving"]
    return ok, "\n".join(out_lines).strip() or ("ok" if ok else "Tailscale didn't turn Serve on.")


# ── app icons (generated once from assets/brinko.png; delete phone_app/icons to rebuild) ─────────────────────────
def _ensure_icons():
    icons = PHONE_DIR / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    src = BASE_DIR / "assets" / "brinko.png"
    try:
        from PIL import Image
        if not src.exists():
            return
        img = Image.open(src).convert("RGBA")
        for size in (192, 512):
            out = icons / f"icon-{size}.png"
            if not out.exists():
                img.resize((size, size), Image.LANCZOS).save(out)
            mask = icons / f"maskable-{size}.png"
            if not mask.exists():   # Android crops maskable icons to a circle/squircle — pad + dark bg
                bg = Image.new("RGBA", (size, size), (5, 13, 63, 255))
                inner = int(size * 0.72)
                bg.alpha_composite(img.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2, (size - inner) // 2))
                bg.save(mask)
    except Exception as e:
        print(f"[Phone] ⚠️ icon generation failed: {e}")


# ── Twilio helpers (optional inbound phone calls) ────────────────────────────
def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _twiml_gather(prompt: str, action_url: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="{action_url}" method="POST" timeout="6" speechTimeout="auto" language="en-US">
    <Say voice="alice">{_xml(prompt)}</Say>
  </Gather>
  <Say voice="alice">I didn't catch that. Call back when you need me.</Say>
  <Hangup/>
</Response>"""


_FAREWELL = {"goodbye", "bye", "that's all", "thats all", "hang up", "end call", "nothing", "no thanks", "i'm done", "im done"}


def _load_twilio_creds():
    try:
        d = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        return d.get("twilio_account_sid"), d.get("twilio_auth_token")
    except Exception:
        return None, None


# ── server ───────────────────────────────────────────────────────────────────
def start_remote_server(ui, port: int = PORT) -> None:
    global ACTIVE_PORT
    ACTIVE_PORT = port
    token = _load_or_create_token()
    twilio_sid, twilio_token = _load_twilio_creds()
    _ensure_icons()
    app = Flask(__name__)
    app.logger.disabled = True
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def authed() -> bool:
        got = (request.headers.get("X-Brinko-Token") or request.args.get("token")
               or (request.get_json(silent=True) or {}).get("token") or "")
        return secrets.compare_digest(str(got), token)

    # app shell
    @app.route("/")
    def index():
        resp = send_from_directory(PHONE_DIR, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/manifest.webmanifest")
    def manifest():
        resp = send_from_directory(PHONE_DIR, "manifest.webmanifest", mimetype="application/manifest+json")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/sw.js")
    def service_worker():
        resp = send_from_directory(PHONE_DIR, "sw.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    # visual effects pages (screensaver, camera powers, document builder) +
    # the hand-tracking engine/models they load
    _FX_TYPES = {".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm",
                 ".task": "application/octet-stream", ".tflite": "application/octet-stream",
                 ".html": "text/html", ".css": "text/css", ".json": "application/json"}

    # camera portal characters (only for the PC's own FX windows, never through
    # Tailscale, which proxies with X-Forwarded-For)
    def _local_only():
        return request.remote_addr in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For")             and not request.headers.get("Tailscale-User-Login")

    @app.route("/fx/api/characters", methods=["GET", "POST"])
    def fx_chars():
        if not _local_only():
            return jsonify({"ok": False, "error": "local only"}), 403
        from actions import characters
        if request.method == "GET":
            characters.upgrade_async()          # re-draw low-res built-ins with FLUX when quota allows
            items = characters.load()
            for c in items:                     # version = file time, so a replaced picture isn't served from cache
                try:
                    c["v"] = int((characters.DIR / c["file"]).stat().st_mtime)
                except OSError:
                    pass
            return jsonify(items)
        return jsonify(characters.add(str((request.get_json(silent=True) or {}).get("name") or "")))

    @app.route("/fx/api/characters/job/<job>")
    def fx_chars_job(job):
        if not _local_only():
            return jsonify({"done": True, "ok": False}), 403
        from actions import characters
        return jsonify(characters.status(job))

    @app.route("/fx/api/characters/<slug>", methods=["DELETE"])
    def fx_chars_delete(slug):
        if not _local_only():
            return jsonify({"ok": False}), 403
        from actions import characters
        return jsonify({"ok": characters.remove(slug)})

    @app.route("/fx/<path:name>")
    def fx(name):
        ext = Path(name).suffix.lower()
        resp = send_from_directory(FX_DIR, name, mimetype=_FX_TYPES.get(ext))
        resp.headers["Cache-Control"] = "no-cache" if ext in (".html", ".js", ".json") else "max-age=86400"
        return resp

    @app.route("/icons/<path:name>")
    def icons(name):
        return send_from_directory(PHONE_DIR / "icons", name)

    # API
    @app.route("/api/ping")
    def ping():
        if not authed():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return jsonify({"ok": True, "pc_ready": BRAIN.brinko is not None})

    @app.route("/api/ask", methods=["POST"])
    def ask():
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "empty"}), 400
        try:
            out = BRAIN.ask(text, data.get("history") or [])
        except Exception as e:
            print(f"[Phone] ⚠️ ask failed: {e}")
            out = {"reply": f"Something went wrong on the PC: {e}", "actions": []}
        return jsonify(out)

    @app.route("/api/kit")
    def kit():
        """Everything the phone needs to keep working when the PC is OFF:
        BRINKO's personality, the user's memories, the voice, the phone tools
        and the Gemini key (so the phone can talk to Google directly)."""
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        from phone_brain import PHONE_TOOLS, KNOWN_APPS, brinko_core_prompt
        from actions.smart_writer import _COMMON_RULES
        try:
            keys = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        except Exception:
            keys = {}
        mem = ""
        try:
            from memory.memory_manager import load_memory, format_memory_for_prompt
            mem = format_memory_for_prompt(load_memory())
        except Exception:
            pass
        return jsonify({
            "version": 1,
            "key": keys.get("gemini_api_key", ""),
            "voice": keys.get("brinko_voice") or "Sulafat",
            "tts_models": ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview"],
            "chat_models": BRAIN.MODELS,
            "persona": brinko_core_prompt(),
            "memory": mem,
            "writer_rules": _COMMON_RULES,
            "tools": PHONE_TOOLS + [{
                "name": "save_memory",
                "description": "Silently remember a personal fact the user reveals (name, people, plans, preferences).",
                "parameters": {"type": "OBJECT", "properties": {
                    "category": {"type": "STRING", "description": "identity | preferences | projects | relationships | wishes | notes"},
                    "key": {"type": "STRING"}, "value": {"type": "STRING"}}, "required": ["category", "key", "value"]},
            }],
            "apps": {k: list(v) for k, v in KNOWN_APPS.items()},
        })

    @app.route("/api/sync", methods=["POST"])
    def sync():
        """Phone was in phone-only mode: hand back what it learned + said."""
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        saved = 0
        try:
            from memory.memory_manager import update_memory
            for m in data.get("memories") or []:
                if m.get("key") and m.get("value"):
                    update_memory({m.get("category") or "notes": {m["key"]: {"value": m["value"]}}})
                    saved += 1
        except Exception as e:
            print(f"[Phone] ⚠️ memory sync failed: {e}")
        if BRAIN.brinko is not None:
            for line in (data.get("conversation") or [])[-12:]:
                BRAIN.brinko._recent.append(str(line)[:300])
        return jsonify({"ok": True, "memories": saved})

    @app.route("/api/events")
    def events():
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        since = int(request.args.get("since", 0) or 0)
        try:
            from agent.background_jobs import get_jobs
            jobs = [j for j in get_jobs().snapshot()][:6]
        except Exception:
            jobs = []
        return jsonify({"events": BRAIN.events_since(since), "jobs": jobs})

    # Twilio inbound calls → same phone brain, answer spoken back on the call
    def _verify_twilio(req) -> bool:
        if not twilio_sid or not twilio_token:
            return False
        try:
            from twilio.request_validator import RequestValidator
            return RequestValidator(twilio_token).validate(
                req.url, req.form.to_dict(), req.headers.get("X-Twilio-Signature", ""))
        except Exception:
            return False

    @app.route("/twilio/voice", methods=["POST"])
    def twilio_voice():
        if not _verify_twilio(request):
            return Response("Forbidden", status=403)
        return Response(_twiml_gather("BRINKO here. Go ahead.", request.host_url.rstrip("/") + "/twilio/action"),
                        mimetype="text/xml")

    @app.route("/twilio/action", methods=["POST"])
    def twilio_action():
        if not _verify_twilio(request):
            return Response("Forbidden", status=403)
        speech = (request.form.get("SpeechResult") or "").strip()
        action_url = request.host_url.rstrip("/") + "/twilio/action"
        if not speech:
            return Response(_twiml_gather("I didn't catch that. What would you like?", action_url), mimetype="text/xml")
        if speech.lower().rstrip(".!?") in _FAREWELL:
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">Bye!</Say><Hangup/></Response>',
                            mimetype="text/xml")
        reply = BRAIN.ask(speech).get("reply", "Done.")
        return Response(_twiml_gather(reply + " Anything else?", action_url), mimetype="text/xml")

    def _run():
        try:
            app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
        except Exception as e:
            print(f"[Phone] ⚠️ server failed to start: {e}")

    threading.Thread(target=_run, daemon=True, name="PhoneServer").start()
    ui.write_log("SYS: 📱 Phone app ready — open Settings → Connect phone to pair.")

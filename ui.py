from __future__ import annotations

import html
import json
import math
import os
import platform
import random
import sys
import threading
import time
from pathlib import Path

from agent.personas import AGENT_RING, AGENT_COLORS, AGENT_SHAPES, AGENT_ROLE, persona_for

# QtWebEngine (FX windows: screensaver, camera, document builder) must be
# imported BEFORE the QApplication exists.
try:
    import PyQt6.QtWebEngineWidgets  # noqa: F401
except Exception as _e:
    print(f"[UI] web effects unavailable: {_e}")

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QFontMetricsF, QKeySequence,
    QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 1040, 760
_MIN_W,     _MIN_H     = 760, 600

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    # Background - deep royal blue (Nov 9 2026 reference: white star in a blue constellation)
    BG        = "#050d3f"
    PANEL     = "#0d1c66"
    PANEL2    = "#13257a"
    BORDER    = "#21409e"
    BORDER_B  = "#4a74e0"
    BORDER_A  = "#2d52c0"
    # Primary - icy light blue
    PRI       = "#7fd6ff"
    PRI_DIM   = "#2d5fb8"
    PRI_GHO   = "#0a1652"
    # Accents
    ACC       = "#4b8dff"   # electric blue
    ACC2      = "#b4e6ff"   # pale ice
    # State colors
    GREEN     = "#4dffb8"
    GREEN_D   = "#1f9e74"
    RED       = "#ff4d6d"
    MUTED_C   = "#8aa3b5"
    ULTRA_A   = "#ffb03a"
    ULTRA_B   = "#ff8a1a"
    # Text
    TEXT      = "#d8ecff"
    TEXT_DIM  = "#4d66ad"
    TEXT_MED  = "#94b3ea"
    WHITE     = "#f3f8ff"
    DARK      = "#050d3f"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h)
    c.setAlpha(max(0, min(255, int(a))))
    return c


_UI_FAMILIES   = ["Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"]
_MONO_FAMILIES = ["Cascadia Mono", "Consolas", "JetBrains Mono", "Menlo", "Courier New"]
_FONT_CACHE: dict[tuple, QFont] = {}


def _font(size: float, weight=QFont.Weight.Normal, mono: bool = False,
          spacing: float = 0.0, px: bool = False) -> QFont:
    """Cached font factory — building QFont objects every frame is wasted work."""
    key = (round(size, 1), weight, mono, spacing, px)
    f = _FONT_CACHE.get(key)
    if f is None:
        f = QFont()
        f.setFamilies(_MONO_FAMILIES if mono else _UI_FAMILIES)
        if px:
            f.setPixelSize(max(1, int(size)))
        else:
            f.setPointSizeF(size)
        f.setWeight(weight)
        if spacing:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        _FONT_CACHE[key] = f
    return f


_GLOW_CACHE: dict[tuple, QPixmap] = {}


def _glow_sprite(color: str, size: int = 128) -> QPixmap:
    """Pre-rendered soft radial glow. Blitting a cached sprite is far cheaper
    than building a QRadialGradient fill for every glow on every frame."""
    key = (color, size)
    px = _GLOW_CACHE.get(key)
    if px is None:
        px = QPixmap(size, size)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(QPointF(size / 2, size / 2), size / 2)
        g.setColorAt(0.0, qcol(color, 255))
        g.setColorAt(0.35, qcol(color, 110))
        g.setColorAt(1.0, qcol(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QRectF(0, 0, size, size))
        p.end()
        _GLOW_CACHE[key] = px
    return px


def _draw_glow(p: QPainter, color: str, cx: float, cy: float, r: float, opacity: float):
    if opacity <= 0.01 or r <= 0.5:
        return
    p.setOpacity(max(0.0, min(1.0, opacity)))
    p.drawPixmap(QRectF(cx - r, cy - r, r * 2, r * 2), _glow_sprite(color),
                 QRectF(0, 0, 128, 128))
    p.setOpacity(1.0)


_TEXT_CACHE: dict[tuple, QPixmap] = {}


def _text_sprite(text: str, font: QFont, color: QColor, dpr: float) -> QPixmap:
    """Text rendered once into a pixmap — drawText every frame is slow."""
    key = (text, font.key(), color.rgba(), round(dpr, 2))
    px = _TEXT_CACHE.get(key)
    if px is None:
        if len(_TEXT_CACHE) > 400:
            _TEXT_CACHE.clear()
        fm = QFontMetricsF(font)
        w, h = fm.horizontalAdvance(text) + 4, fm.height() + 2
        px = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(font)
        p.setPen(color)
        p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        _TEXT_CACHE[key] = px
    return px


def _draw_text_centered(p: QPainter, text: str, font: QFont, color: QColor,
                        cx: float, cy: float, dpr: float):
    px = _text_sprite(text, font, color, dpr)
    w, h = px.width() / dpr, px.height() / dpr
    p.drawPixmap(QPointF(cx - w / 2, cy - h / 2), px)


def _regular_polygon(cx: float, cy: float, r: float, sides: int, rot_deg: float = 0) -> QPolygonF:
    pts = []
    for i in range(sides):
        ang = math.radians(rot_deg + i * 360 / sides - 90)
        pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return QPolygonF(pts)


def _star_polygon(cx: float, cy: float, r: float, points: int = 5, rot_deg: float = 0) -> QPolygonF:
    pts = []
    inner = r * 0.46
    for i in range(points * 2):
        rad = r if i % 2 == 0 else inner
        ang = math.radians(rot_deg + i * 360 / (points * 2) - 90)
        pts.append(QPointF(cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    return QPolygonF(pts)


def draw_agent_shape(p: QPainter, shape: str, cx: float, cy: float, r: float):
    """Draws the current pen/brush into one of the agent-node shapes —
    each agent gets a genuinely different silhouette, not just a color."""
    if shape == "square":
        s = r * 0.86
        p.drawRoundedRect(QRectF(cx - s, cy - s, s * 2, s * 2), s * 0.18, s * 0.18)
    elif shape == "rounded_square":
        s = r * 0.88
        p.drawRoundedRect(QRectF(cx - s, cy - s, s * 2, s * 2), s * 0.45, s * 0.45)
    elif shape == "diamond":
        p.drawPolygon(_regular_polygon(cx, cy, r * 1.05, 4, rot_deg=0))
    elif shape == "triangle":
        p.drawPolygon(_regular_polygon(cx, cy + r * 0.12, r * 1.15, 3))
    elif shape == "pentagon":
        p.drawPolygon(_regular_polygon(cx, cy, r * 1.02, 5))
    elif shape == "hexagon":
        p.drawPolygon(_regular_polygon(cx, cy, r * 1.02, 6))
    elif shape == "octagon":
        p.drawPolygon(_regular_polygon(cx, cy, r, 8, rot_deg=22.5))
    elif shape == "star":
        p.drawPolygon(_star_polygon(cx, cy, r * 1.15, points=5))
    elif shape == "ring":
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.drawEllipse(QPointF(cx, cy), r * 0.62, r * 0.62)
    else:
        p.drawEllipse(QPointF(cx, cy), r, r)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _ease_out(x: float) -> float:
    x = _clamp01(x)
    return 1 - (1 - x) ** 3


def _ease_back(x: float, s: float = 1.7) -> float:
    x = _clamp01(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


AMBER = "#ffb347"

_STATE_STYLE = {
    "LISTENING":    ("Listening",   C.PRI),
    "SPEAKING":     ("Speaking",    C.ACC2),
    "THINKING":     ("Thinking",    C.WHITE),
    "PROCESSING":   ("Working",     C.WHITE),
    "MUTED":        ("Mic off",     C.MUTED_C),
    "STANDBY":      ("Standby · say “Hey Jarvis”", C.ULTRA_B),
    "INITIALISING": ("Starting up", C.PRI),
}

import numpy as _np


def _voice_name() -> str:
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8")).get("brinko_voice") or DEFAULT_VOICE
    except Exception:
        return DEFAULT_VOICE


DEFAULT_VOICE = "Sulafat"       # warm, natural female voice (Gemini Live prebuilt)
FEMALE_VOICES = ["Sulafat", "Aoede", "Achernar", "Despina", "Leda", "Kore"]
OTHER_VOICES = ["Puck", "Charon", "Fenrir", "Orus"]

_MARKETS: dict = {}
_MARKETS_STARTED = False


def _start_markets():
    """Crypto prices + 7-day sparklines for the MARKETS card, refreshed every 10 min
    (CoinGecko public API, no key). Runs on its own thread; the HUD just reads _MARKETS."""
    global _MARKETS_STARTED
    if _MARKETS_STARTED:
        return
    _MARKETS_STARTED = True

    def loop():
        import urllib.request
        url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
               "&ids=bitcoin,ethereum,solana&sparkline=true&price_change_percentage=24h")
        badges = {"btc": "#f7931a", "eth": "#627eea", "sol": "#9945ff"}
        while True:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "BRINKO-HUD"})
                d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
                rows = []
                for c in d:
                    sp = (c.get("sparkline_in_7d") or {}).get("price") or []
                    step = max(1, len(sp) // 36)
                    pr = c.get("current_price") or 0
                    rows.append({"name": c["name"], "sym": c["symbol"].upper(), "badge": badges.get(c["symbol"], "#4b8dff"),
                                 "price": f"${pr:,.0f}" if pr >= 100 else f"${pr:,.2f}",
                                 "chg": float(c.get("price_change_percentage_24h") or 0.0), "spark": sp[::step]})
                _MARKETS["rows"] = rows
                time.sleep(600)
            except Exception as e:
                print(f"[HUD] markets: {e}")
                time.sleep(120)
    threading.Thread(target=loop, daemon=True, name="Markets").start()


class _ParticleCloud:
    """~1500 particles in a wide 3D cloud around the sphere. Rotation + perspective are
    done in numpy; drawing is a handful of batched drawPoints/drawLines calls."""
    def __init__(self, n: int = 1800, seed: int = 7):
        rng = _np.random.default_rng(seed)
        v = rng.normal(size=(n, 3))
        v /= _np.linalg.norm(v, axis=1, keepdims=True)
        r = 1.3 + 5.6 * rng.random(n) ** 1.7            # in sphere radii, denser near the sphere
        v[:, 1] *= 0.6                                    # wider than tall, like the reference
        self.p = v * r[:, None]
        self.r = r
        self.phase = rng.random(n) * 6.283
        self.big = rng.random(n) > 0.86
        self.tint = rng.random(n)
        # constellation lines: a few hundred outer points joined to their 2 nearest neighbours
        # (fixed in 3D, so the web turns with the cloud like in the reference)
        pick = _np.nonzero((r > 1.7) & (r < 4.8) & (rng.random(n) < 0.4))[0]
        P = self.p[pick]
        d = _np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
        _np.fill_diagonal(d, 1e9)
        nn = _np.argsort(d, axis=1)[:, :2]
        e = set()
        for i in range(len(pick)):
            for j in nn[i]:
                if d[i, j] < 0.75:
                    e.add((min(pick[i], pick[j]), max(pick[i], pick[j])))
        e = _np.array(sorted(e), dtype=int).reshape(-1, 2)
        self.e0, self.e1 = e[:, 0], e[:, 1]

    def project(self, cx, cy, R, ang, tilt=0.32):
        x, y, z = self.p[:, 0], self.p[:, 1], self.p[:, 2]
        ca, sa = _np.cos(ang), _np.sin(ang)
        x1, z1 = x * ca - z * sa, x * sa + z * ca
        ct, st = _np.cos(tilt), _np.sin(tilt)
        y2, z2 = y * ct - z1 * st, y * st + z1 * ct
        D = 10.0
        k = D / (D + z2)
        return cx + x1 * k * R, cy + y2 * k * R, z2, k


def _mix(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(int(c1.red() + (c2.red() - c1.red()) * t), int(c1.green() + (c2.green() - c1.green()) * t),
                  int(c1.blue() + (c2.blue() - c1.blue()) * t))


_STATE_ACCENT = {
    "LISTENING": "#6fcfff", "SPEAKING": "#9fe4ff", "THINKING": "#b3bcff", "PROCESSING": "#b3bcff",
    "INITIALISING": "#b3bcff", "STANDBY": "#ffb547", "MUTED": "#8a9cc8", "ULTRA": "#ffb03a",
}


class HudCanvas(QWidget):
    """The main HUD: glass orb, voice-reactive ring, agent constellation,
    top bar, and live background-job cards.

    Performance notes:
      • Everything static (gradient, dot grid, tick ring, orbit path) is
        rendered once into a cached pixmap and blitted each frame.
      • Animation is time-based, so the frame rate can drop to ~30fps when
        idle (and to nothing while minimized) without anything slowing down.
      • No per-frame QFont / pixmap rescaling / subprocess work.
    """
    orb_tapped      = pyqtSignal()
    file_dropped    = pyqtSignal(str)
    settings_tapped = pyqtSignal()

    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self.ultra    = False
        self.bottom_inset = 0

        self._last  = time.time()
        self._t     = 0.0
        self._mic_level = 0.0
        self._out_level = 0.0
        self._level     = 0.0     # smoothed voice level driving the ring
        self._energy    = 0.3     # smoothed overall "activity"
        self._rot       = 0.0

        self._particles: list[list[float]] = []
        self._flame:     list[list[float]] = []
        self._active_agent: str | None = None
        self._active_agent_until = 0.0
        self._jobs: list[dict] = []
        self._jobs_rx_t  = time.time()
        self._finished_at: dict[int, float] = {}
        self._drag_over  = False
        self._hover_pt   = QPointF(-1, -1)
        self._hover_agent: str | None = None
        self._node_pos: list[tuple[str, float, float, float]] = []

        self._born        = time.time()     # boot animation clock
        self._ripples: list[float] = []     # sonar rings while the user talks (start times)
        self._last_ripple = 0.0
        self._beams: list[tuple[str, float]] = []           # (agent, start) orb → agent
        self._bursts: list[tuple[str, float, str]] = []     # (agent, start, colour) job done
        self._hover_orb   = False
        self._pill_w      = 0.0
        self._comp_key = None
        self._comp_at = 0.0

        self._bg_px: QPixmap | None = None
        self._bg_key = None
        self._cloud = _ParticleCloud()
        self._acc = QColor(_STATE_ACCENT["THINKING"])
        self._warp = 0.0
        self._spin = 0.0
        self._logs: list[tuple[float, str, str]] = []      # (time, kind, text) for the left column
        self._agent_tag: tuple[str, float] | None = None
        self._face_src: QPixmap | None = None
        self._face_px:  QPixmap | None = None
        self._face_d = 0
        if face_path:
            self._load_face(face_path)

        self._wave: list[float] = [0.0] * 72          # voice bar history
        self._wave_t = 0.0
        self._stats: dict = {}
        self.voice_name = _voice_name()
        self._stats_tmr = QTimer(self)
        self._stats_tmr.timeout.connect(self._poll_stats)
        self._stats_tmr.start(2000)
        self._poll_stats()
        _start_markets()

        self._tmr = QTimer(self)
        self._tmr.setTimerType(Qt.TimerType.PreciseTimer)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def _poll_stats(self):
        try:
            import psutil
            self._stats["cpu"] = psutil.cpu_percent(None)
            self._stats["ram"] = psutil.virtual_memory().percent
            b = psutil.sensors_battery()
            self._stats["bat"] = b.percent if b else None
        except Exception:
            pass
        self.voice_name = _voice_name()

    # ── public setters ──────────────────────────────────────────────────
    def set_mic_level(self, level: float):
        self._mic_level = max(0.0, min(1.0, float(level)))

    def set_out_level(self, level: float):
        self._out_level = max(0.0, min(1.0, float(level)))

    def push_log(self, text: str):
        """Left log column: every log line, tool call and status change, newest at the bottom."""
        t = (text or "").strip()
        if not t:
            return
        kind = "sys"
        low = t.lower()
        if low.startswith("you:"):
            kind, t = "you", t[4:].strip()
        elif low.startswith("brinko:"):
            kind, t = "brinko", t[6:].strip()
        elif low.startswith("sys:"):
            t = t[4:].strip()
        elif low.startswith("file:"):
            kind, t = "file", t[5:].strip()
        self._logs.append((time.time(), kind, t.replace("\n", " ")[:140]))
        self._logs = self._logs[-60:]
        self._wake()

    def flash_agent(self, name: str, ms: int = 1100):
        self._agent_tag = (name, time.time())
        self.push_log(f"SYS: {name} \u25b8 {AGENT_ROLE.get(name, 'agent')}")
        if name in AGENT_RING:
            self._beams.append((name, time.time()))
            self._beams = self._beams[-6:]
        self._active_agent = name
        self._active_agent_until = time.time() + (ms / 1000.0)
        self._wake()

    def set_ultra(self, on: bool):
        self.ultra = bool(on)
        self._wake()

    def set_jobs(self, jobs: list):
        now = time.time()
        for j in jobs:
            if j.get("status") != "running" and j["id"] not in self._finished_at:
                self._finished_at[j["id"]] = now
                self._bursts.append((persona_for(j["tool"]), now,
                                     C.GREEN if j.get("status") == "done" else C.RED))
        self._jobs = list(jobs)
        self._jobs_rx_t = now
        self._wake()

    def _wake(self):
        if self._tmr.interval() > 20:
            self._tmr.setInterval(20)
        self.update()

    # ── geometry ────────────────────────────────────────────────────────
    def _geom(self):
        W, H = self.width(), self.height()
        top    = 64
        bottom = self.bottom_inset + 12
        avail  = max(220, H - top - bottom)
        cx, cy = W / 2, top + avail / 2
        fw     = min(W * 0.64, avail * 0.90)
        return W, H, cx, cy, fw

    def _orb_r(self, fw: float) -> float:
        return fw * 0.19

    def _visible_jobs(self) -> list[dict]:
        now = time.time()
        out = [j for j in self._jobs if j.get("status") == "running"]
        out += [j for j in self._jobs if j.get("status") != "running"
                and now - self._finished_at.get(j["id"], now) < 8.0]
        return out[:4]

    def _busy_agents(self) -> set[str]:
        return {persona_for(j["tool"]) for j in self._jobs if j.get("status") == "running"}

    # ── input ───────────────────────────────────────────────────────────
    def _gear_rect(self) -> QRectF:
        return QRectF(14, 14, 36, 36)

    def mousePressEvent(self, e):
        pos = e.position()
        if self._gear_rect().contains(pos):
            self.settings_tapped.emit()
            return
        W, H, cx, cy, fw = self._geom()
        if math.hypot(pos.x() - cx, pos.y() - cy) <= self._orb_r(fw) * 1.35:
            self.orb_tapped.emit()

    def mouseMoveEvent(self, e):
        pos = e.position()
        self._hover_pt = pos
        W, H, cx, cy, fw = self._geom()
        hover = None
        for name, nx, ny, nr in self._node_pos:
            if math.hypot(pos.x() - nx, pos.y() - ny) <= nr * 1.9:
                hover = name
                break
        on_orb  = math.hypot(pos.x() - cx, pos.y() - cy) <= self._orb_r(fw) * 1.35
        if on_orb != self._hover_orb:
            self._hover_orb = on_orb
            self._wake()
        on_gear = self._gear_rect().contains(pos)
        self.setCursor(Qt.CursorShape.PointingHandCursor if (on_orb or on_gear)
                       else Qt.CursorShape.ArrowCursor)
        if hover != self._hover_agent:
            self._hover_agent = hover
            self._wake()

    def leaveEvent(self, e):
        self._hover_pt = QPointF(-1, -1)
        self._hover_agent = None
        self.update()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True
            self._wake()

    def dragLeaveEvent(self, e):
        self._drag_over = False
        self.update()

    def dropEvent(self, e):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path:
                self.file_dropped.emit(path)
        self.update()

    def resizeEvent(self, e):
        self._bg_px = None
        super().resizeEvent(e)

    # ── assets ──────────────────────────────────────────────────────────
    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.crop(((img.width - sz) // 2, (img.height - sz) // 2,
                            (img.width + sz) // 2, (img.height + sz) // 2))
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_src = px
        except Exception:
            self._face_src = None

    def _face_for(self, d: int) -> QPixmap | None:
        if not self._face_src:
            return None
        if self._face_px is None or abs(self._face_d - d) > 4:
            dpr = self.devicePixelRatioF()
            px = self._face_src.scaled(
                int(d * dpr), int(d * dpr),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            px.setDevicePixelRatio(dpr)
            self._face_px, self._face_d = px, d
        return self._face_px

    def _build_bg(self):
        W, H, cx, cy, fw = self._geom()
        dpr = self.devicePixelRatioF()
        px = QPixmap(max(1, int(W * dpr)), max(1, int(H * dpr)))
        px.setDevicePixelRatio(dpr)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(QRectF(0, 0, W, H), qcol(C.BG))
        g = QRadialGradient(QPointF(cx, cy), max(W, H) * 0.78)
        g.setColorAt(0.0, qcol("#1c3fd0"))
        g.setColorAt(0.22, qcol("#14309f"))
        g.setColorAt(0.55, qcol("#0b1d70"))
        g.setColorAt(1.0, qcol("#040b37"))
        p.fillRect(QRectF(0, 0, W, H), QBrush(g))
        # far, still stars
        rng = random.Random(11)
        p.setPen(Qt.PenStyle.NoPen)
        for _ in range(int(W * H / 5200)):
            x, y = rng.random() * W, rng.random() * H
            a = rng.randint(25, 90)
            p.setBrush(qcol("#bfe0ff", a))
            r_ = rng.choice((0.6, 0.6, 0.8, 1.1))
            p.drawEllipse(QPointF(x, y), r_, r_)
        # soft edge vignette
        v = QRadialGradient(QPointF(cx, cy), max(W, H) * 0.75)
        v.setColorAt(0.6, qcol("#000000", 0))
        v.setColorAt(1.0, qcol("#000014", 120))
        p.fillRect(QRectF(0, 0, W, H), QBrush(v))
        p.end()
        self._bg_px = px
        self._bg_key = (W, H, self.bottom_inset)

    # ── animation step ──────────────────────────────────────────────────
    def _step(self):
        win = self.window()
        hidden = win is not None and (win.isMinimized() or not win.isVisible())
        mini = getattr(self, "mini", None)
        if hidden and not (mini and mini.isVisible()):
            if self._tmr.interval() != 300:
                self._tmr.setInterval(300)
            return

        now = time.time()
        dt  = min(0.1, now - self._last)
        self._last = now
        self._t += dt
        k = dt * 60.0

        if self.speaking:
            target = self._out_level
        elif self.state == "LISTENING" and not self.muted:
            target = self._mic_level
        else:
            target = 0.0
        rate = 0.5 if target > self._level else 0.12
        self._level += (target - self._level) * min(1.0, rate * k)
        if now - self._wave_t > 0.045:                    # ~22 samples a second scroll through the voice bar
            self._wave_t = now
            self._wave = self._wave[1:] + [min(1.0, target * 1.6)]

        if self.speaking:
            tgt_e = 1.0
        elif self.state in ("THINKING", "PROCESSING"):
            tgt_e = 0.6
        elif self.muted:
            tgt_e = 0.08
        else:
            tgt_e = 0.3
        self._energy += (tgt_e - self._energy) * min(1.0, 0.05 * k)
        # NO '% 360' here: things rotate at different multiples of _rot (0.12x,
        # -0.55x, 1.6x), so wrapping at 360 made them jump back every lap.
        # Each use wraps its own angle instead.
        self._rot = (self._rot + (6 + 28 * self._energy) * dt) % 360000.0
        key = ("MUTED" if self.muted else "ULTRA" if self.ultra else "SPEAKING" if self.speaking else self.state)
        self._acc = _mix(self._acc, QColor(_STATE_ACCENT.get(key, "#3fe6ff")), 0.08 * k)
        warp_t = min(0.4, self._out_level * 1.2) if self.speaking else 0.0   # gentle: the star does the talking
        self._warp += (warp_t - self._warp) * min(1.0, (0.25 if warp_t > self._warp else 0.06) * k)
        self._spin += dt * (0.05 + 0.10 * self._energy + 0.5 * self._warp)

        W, H, cx, cy, fw = self._geom()
        r_orb = self._orb_r(fw)
        if self.speaking and self._level > 0.12 and len(self._particles) < 40 \
                and random.random() < 0.4 * k:
            ang = random.uniform(0, 2 * math.pi)
            sp  = random.uniform(0.6, 1.8)
            self._particles.append([cx + math.cos(ang) * r_orb * 1.2,
                                    cy + math.sin(ang) * r_orb * 1.2,
                                    math.cos(ang) * sp, math.sin(ang) * sp, 1.0])
        if self._particles:
            self._particles = [
                [q[0] + q[2] * k, q[1] + q[3] * k, q[2], q[3], q[4] - 0.022 * k]
                for q in self._particles if q[4] > 0
            ]

        if self.ultra:
            if len(self._flame) < 60 and random.random() < 0.6 * k:
                ang = random.uniform(0, 2 * math.pi)
                rb  = fw * 0.35
                self._flame.append([cx + math.cos(ang) * rb, cy + math.sin(ang) * rb,
                                    math.cos(ang) * 0.5, -random.uniform(0.6, 1.4),
                                    1.0, random.uniform(3, 7)])
            self._flame = [[q[0] + q[2] * k, q[1] + q[3] * k, q[2], q[3], q[4] - 0.02 * k, q[5]]
                           for q in self._flame if q[4] > 0]
        elif self._flame:
            self._flame = []

        if self._active_agent and now > self._active_agent_until:
            self._active_agent = None

        # sonar ripples while the user is talking
        if (self.state == "LISTENING" and not self.muted and self._mic_level > 0.16
                and now - self._last_ripple > 0.42):
            self._ripples.append(now)
            self._last_ripple = now
        self._ripples = [r for r in self._ripples if now - r < 1.5]
        self._beams   = [b for b in self._beams if now - b[1] < 1.1]
        self._bursts  = [b for b in self._bursts if now - b[1] < 1.4]
        booting = now - self._born < 3.2

        running = any(j.get("status") == "running" for j in self._jobs)
        recent  = any(now - t < 8.5 for t in self._finished_at.values())
        busy = (self.speaking or self.state in ("THINKING", "PROCESSING") or running
                or recent or self.ultra or self._drag_over or self._particles
                or self._active_agent or self._level > 0.02 or self._hover_agent
                or booting or self._ripples or self._beams or self._bursts or self._hover_orb)
        # ~50 fps when busy, 30 idle: smooth, and leaves the GIL free for the audio threads
        iv = 20 if (busy or self._warp > 0.02) else 33
        if hidden:                       # minimized: only the floating mini orb is drawn
            iv = 33
            mini.update()
        if self._tmr.interval() != iv:
            self._tmr.setInterval(iv)
        if not hidden:
            self.update()

    # ── painting ────────────────────────────────────────────────────────
    def paintEvent(self, _):
        W, H, cx, cy, fw = self._geom()
        if self._bg_px is None or self._bg_key != (W, H, self.bottom_inset):
            self._build_bg()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, self._bg_px)

        now  = time.time()
        boot = now - self._born
        acc  = self._acc
        lvl, en, t = self._level, self._energy, self._t
        grow = _ease_back((boot - 0.1) / 1.0, 1.6)
        R = self._orb_r(fw) * (1.0 + 0.07 * lvl) * max(0.001, grow)

        self._paint_cloud(p, W, H, cx, cy, self._orb_r(fw), acc, boot)
        self._paint_sphere(p, cx, cy, R, acc, lvl, en, t)

        # sonar ripples while you talk
        for r0 in self._ripples:
            a = (now - r0) / 1.5
            rr = R * (1.15 + 1.6 * _ease_out(a))
            p.setPen(QPen(qcol(acc.name(), int(110 * (1 - a))), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # agent at work: small tag under the sphere
        if self._agent_tag and now - self._agent_tag[1] < 2.2:
            name, t0 = self._agent_tag
            a = min(1.0, (now - t0) / 0.15) * min(1.0, (2.2 - (now - t0)) / 0.5)
            dpr = self.devicePixelRatioF()
            tag = _text_sprite(f"{name}  \u00b7  {AGENT_ROLE.get(name, '').upper()}",
                               _font(8, QFont.Weight.DemiBold, mono=True, spacing=2.0), qcol(acc.name(), 230), dpr)
            p.setOpacity(a)
            p.drawPixmap(QPointF(cx - tag.width() / dpr / 2, cy + R * 1.32), tag)
            p.setOpacity(1.0)

        p.setOpacity(_ease_out((boot - 0.6) / 0.8))
        side = W >= 900
        colw = min(290.0, W * 0.24)
        logs_top = 78.0
        if side:
            rows = _MARKETS.get("rows") or []
            mb = self._cached_panel(p, "markets", tuple((r_["price"], r_["chg"]) for r_ in rows), 16, 70, colw,
                                    30 + 30 * len(rows), lambda q, x0, y0: self._paint_markets(q, x0, y0, colw) - y0)
            logs_top = 70 + mb + 30 if rows else 78.0
        self._paint_logs(p, W, H, acc, logs_top)
        self._paint_top_bar(p, W)
        jobs_top = 70.0
        if side:
            st = self._stats
            gk = (int(st.get("cpu", 0)), int(st.get("ram", 0)), st.get("bat"), time.strftime("%H:%M"))
            jobs_top = 70 + self._cached_panel(p, "gauges", gk, W - colw - 16, 70, colw, 92,
                                               lambda q, x0, y0: self._paint_gauges(q, x0, y0, colw) - y0) + 12
        self._paint_voice_bar(p, W, H, acc)
        p.setOpacity(1.0)
        jobs_bottom = self._paint_jobs(p, W, jobs_top, colw if side else None)
        if side:
            p.setOpacity(_ease_out((boot - 0.6) / 0.8))
            c_top, c_bot = jobs_bottom + 6, H - self.bottom_inset - 70
            said = [(t0_, k_, x_) for (t0_, k_, x_) in self._logs if k_ in ("you", "brinko")][-5:]
            if said and now - said[-1][0] < 0.3:          # newest card is fading in: draw live
                self._paint_chat_cards(p, W - colw - 16, c_top, c_bot, colw)
            elif said and c_bot - c_top > 40:
                self._cached_panel(p, "chat", tuple((t0_, x_) for t0_, _, x_ in said), W - colw - 16, c_top, colw,
                                   c_bot - c_top, lambda q, x0, y0: self._paint_chat_cards(q, x0, y0, y0 + c_bot - c_top, colw))
            p.setOpacity(1.0)

        if self._drag_over:
            dr = R * 1.6
            pen = QPen(qcol(C.GREEN, 220), 2.0, Qt.PenStyle.DashLine)
            pen.setDashOffset(-t * 12)
            p.setPen(pen)
            p.setBrush(qcol(C.GREEN, 18))
            p.drawEllipse(QPointF(cx, cy), dr, dr)
            p.setFont(_font(10, QFont.Weight.DemiBold, spacing=1.0))
            p.setPen(qcol(C.GREEN, 240))
            p.drawText(QRectF(cx - 160, cy + dr + 10, 320, 24), Qt.AlignmentFlag.AlignCenter, "Drop to hand it to BRINKO")
        p.end()

    def _paint_cloud(self, p: QPainter, W, H, cx, cy, R, acc: QColor, boot: float):
        X, Y, Z, K = self._cloud.project(cx, cy, R, self._spin)
        cl = self._cloud
        tw = 0.65 + 0.35 * _np.sin(cl.phase + self._t * 1.7)
        depth = _np.clip(0.95 - (Z + 5.5) / 13.0, 0.08, 1.0)
        a = _np.clip(depth * tw * _np.clip(1.45 - cl.r / 6.4, 0.2, 1.0) * 1.35, 0, 1)
        a *= min(1.0, max(0.0, (boot - 0.2) / 1.2))
        # hidden behind the sphere
        behind = (Z > 0) & ((X - cx) ** 2 + (Y - cy) ** 2 < (R * 0.98) ** 2)
        a = _np.where(behind, a * 0.18, a)
        inside = (X > -4) & (X < W + 4) & (Y > 54) & (Y < H - self.bottom_inset + 6)
        warp = self._warp
        base = _mix(acc, QColor("#ffffff"), 0.15)
        light = _mix(acc, QColor("#ffffff"), 0.6)
        levels = (0.16, 0.32, 0.52, 0.75, 1.0)
        q = _np.digitize(a, levels[:-1])
        # constellation web (thin, aliased: hundreds of AA lines would cost ~10x)
        if warp < 0.5 and len(cl.e0):
            ea = _np.minimum(a[cl.e0], a[cl.e1]) * (1 - warp * 2)
            ok = (ea > 0.12) & inside[cl.e0] & inside[cl.e1]
            sel = _np.nonzero(ok)[0]
            if len(sel):
                strong = ea[sel] > 0.45
                p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                for st, alpha in ((False, 34), (True, 62)):
                    ss = sel[strong == st]
                    if not len(ss):
                        continue
                    p.setPen(QPen(qcol(_mix(acc, QColor("#ffffff"), 0.35).name(), alpha), 1))
                    i0, i1 = cl.e0[ss], cl.e1[ss]
                    p.drawLines([QLineF(a1, b1, a2, b2) for a1, b1, a2, b2 in
                                 zip(X[i0].tolist(), Y[i0].tolist(), X[i1].tolist(), Y[i1].tolist())])
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if warp > 0.03:
            dx, dy = X - cx, Y - cy
            d = _np.hypot(dx, dy) + 1e-3
            ln = warp * (5 + 34 * (cl.r / 6.9)) * K
            X2, Y2 = X + dx / d * ln, Y + dy / d * ln
        for qi, lv in enumerate(levels):
            for big in (False, True):
                sel = _np.nonzero(inside & (q == qi) & (cl.big == big) & (a > 0.03))[0]
                if not len(sel):
                    continue
                col = light if big else base
                alpha = int(255 * lv * (0.95 if big else 0.8))
                xs, ys = X[sel].tolist(), Y[sel].tolist()
                if warp > 0.03 and lv > 0.2:
                    # streaks (front, visible particles only): aliased + flat caps (anti-aliased lines cost ~10x more; at this
                    # length and speed nobody can tell)
                    pen = QPen(qcol(col.name(), alpha), 2 if big else 1)
                    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                    p.setPen(pen)
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    x2, y2 = X2[sel].tolist(), Y2[sel].tolist()
                    p.drawLines([QLineF(a1, b1, a2, b2) for a1, b1, a2, b2 in zip(xs, ys, x2, y2)])
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                elif warp > 0.03:
                    continue
                else:
                    # small dots: crisp aliased pixels (that's what the reference cloud looks like,
                    # and 3x cheaper); the few big ones stay round + smooth
                    pen = QPen(qcol(col.name(), alpha), 2.3 if big else 1.6)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap if big else Qt.PenCapStyle.SquareCap)
                    p.setPen(pen)
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, big)
                    p.drawPoints(QPolygonF([QPointF(a1, b1) for a1, b1 in zip(xs, ys)]))
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def _paint_sphere(self, p: QPainter, cx, cy, R, acc: QColor, lvl, en, t):
        """White-hot star with a blue halo and a thin lens flare (the reference look).
        Everything fits inside 2.6 R (the mini orb relies on that)."""
        an = acc.name()
        pulse = 0.5 + 0.5 * math.sin(t * 1.3)
        halo = _mix(QColor("#2f6dff"), acc, 0.35).name()
        _draw_glow(p, halo, cx, cy, R * (2.45 + 0.15 * pulse + 0.25 * lvl), 0.55 + 0.25 * en)
        _draw_glow(p, _mix(acc, QColor("#ffffff"), 0.35).name(), cx, cy, R * (1.35 + 0.35 * lvl), 0.75 + 0.2 * en)
        p.drawPixmap(QRectF(cx - R, cy - R, R * 2, R * 2), self._star_core(R, an),
                     QRectF(0, 0, self._core_px.width(), self._core_px.height()))
        # the core swells with the voice
        cr = R * (0.46 + 0.03 * pulse + 0.2 * lvl)
        _draw_glow(p, "#ffffff", cx, cy, cr * 1.6, 0.9)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol("#ffffff", 250))
        p.drawEllipse(QPointF(cx, cy), cr * 0.72, cr * 0.72)
        # lens flare: a long thin horizontal streak + a faint short vertical one
        fl = R * (2.3 + 0.6 * lvl)
        for (dx, dy, L, wdt, a0) in ((1, 0, fl, 1.6, 200), (0, 1, fl * 0.38, 1.0, 90)):
            g = QLinearGradient(QPointF(cx - dx * L, cy - dy * L), QPointF(cx + dx * L, cy + dy * L))
            g.setColorAt(0.0, qcol("#ffffff", 0))
            g.setColorAt(0.5, qcol("#ffffff", int(a0 * (0.75 + 0.25 * en))))
            g.setColorAt(1.0, qcol("#ffffff", 0))
            pen = QPen(QBrush(g), wdt)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawLine(QPointF(cx - dx * L, cy - dy * L), QPointF(cx + dx * L, cy + dy * L))

    def _star_core(self, R: float, an: str) -> QPixmap:
        """Bright white disc fading through ice-blue into the halo, cached."""
        key = (int(R / 3), an)
        if getattr(self, "_core_key", None) == key:
            return self._core_px
        dpr = self.devicePixelRatioF()
        S = R * 2
        px = QPixmap(max(1, int(S * dpr)), max(1, int(S * dpr)))
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.GlobalColor.transparent)
        q = QPainter(px)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(S / 2, S / 2)
        g = QRadialGradient(c, R)
        ice = _mix(QColor(an), QColor("#ffffff"), 0.55).name()
        g.setColorAt(0.0, qcol("#ffffff", 255))
        g.setColorAt(0.36, qcol("#ffffff", 245))
        g.setColorAt(0.48, qcol(ice, 200))
        g.setColorAt(0.68, qcol(an, 80))
        g.setColorAt(1.0, qcol(an, 0))
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(g))
        q.drawEllipse(c, R, R)
        q.end()
        self._core_px, self._core_key = px, key
        return px

    def _cached_panel(self, p: QPainter, name: str, key, x: float, y: float, w: float, h: float, fn):
        """Draw a panel through a pixmap cache: rebuilt only when `key` changes (panels update every
        few seconds, the HUD repaints 30-50x a second). fn(painter, x0, y0) draws at the offset and
        returns its result, which is passed through."""
        cache = self.__dict__.setdefault("_pcache", {})
        ent = cache.get(name)
        dpr = self.devicePixelRatioF()
        full = (key, round(w), round(h), dpr)
        if ent is None or ent[0] != full:
            px = QPixmap(max(1, int((w + 4) * dpr)), max(1, int((h + 4) * dpr)))
            px.setDevicePixelRatio(dpr)
            px.fill(Qt.GlobalColor.transparent)
            q = QPainter(px)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            res = fn(q, 2.0, 2.0)
            q.end()
            ent = cache[name] = (full, px, res)
        p.drawPixmap(QPointF(x - 2, y - 2), ent[1])
        return ent[2]

    # ── glass panels (reference: translucent blue cards with a thin light edge) ──
    def _glass(self, p: QPainter, r: QRectF, fade: float = 1.0, radius: float = 12.0):
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, qcol("#2f58dc", int(200 * fade)))
        g.setColorAt(1.0, qcol("#17339f", int(190 * fade)))
        p.setPen(QPen(qcol("#8fb4ff", int(80 * fade)), 1))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, radius, radius)
        p.setPen(QPen(qcol("#d6e6ff", int(55 * fade)), 1))
        p.drawLine(QPointF(r.left() + radius, r.top() + 0.5), QPointF(r.right() - radius, r.top() + 0.5))

    def _panel_label(self, p: QPainter, x: float, y: float, text: str, right: str = "", w: float = 0, col=None):
        dpr = self.devicePixelRatioF()
        f = _font(6.8, QFont.Weight.DemiBold, mono=True, spacing=1.6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol(col or "#7fd6ff", 220))
        p.drawEllipse(QPointF(x + 3, y), 2.2, 2.2)
        lab = _text_sprite(text, f, qcol(C.TEXT_MED, 220), dpr)
        p.drawPixmap(QPointF(x + 10, y - lab.height() / dpr / 2), lab)
        if right and w:
            rl = _text_sprite(right, f, qcol(col or C.TEXT_MED, 230), dpr)
            p.drawPixmap(QPointF(x + w - rl.width() / dpr, y - rl.height() / dpr / 2), rl)

    def _paint_markets(self, p: QPainter, x: float, y: float, w: float) -> float:
        """Live crypto prices with 7-day sparklines (CoinGecko, free, no key). Returns the bottom."""
        rows = _MARKETS.get("rows") or []
        if not rows:
            return y
        h = 30 + 30 * len(rows)
        self._glass(p, QRectF(x, y, w, h))
        self._panel_label(p, x + 12, y + 14, "MARKETS", "7D", w - 24)
        dpr = self.devicePixelRatioF()
        fn = _font(8.4, QFont.Weight.DemiBold)
        fs = _font(6.8, QFont.Weight.Normal, mono=True)
        fp = _font(8.4, QFont.Weight.DemiBold, mono=True)
        yy = y + 30
        for r_ in rows:
            up = r_["chg"] >= 0
            colr = "#4dffb8" if up else "#ff6b8a"
            # coin badge
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcol(r_["badge"], 230))
            p.drawRoundedRect(QRectF(x + 12, yy + 4, 18, 18), 5, 5)
            ini = _text_sprite(r_["sym"][:1], _font(7.5, QFont.Weight.Bold), qcol("#ffffff", 240), dpr)
            p.drawPixmap(QPointF(x + 21 - ini.width() / dpr / 2, yy + 13 - ini.height() / dpr / 2), ini)
            nm = _text_sprite(r_["name"], fn, qcol(C.WHITE, 235), dpr)
            p.drawPixmap(QPointF(x + 36, yy + 2), nm)
            sy = _text_sprite(r_["sym"], fs, qcol(C.TEXT_MED, 200), dpr)
            p.drawPixmap(QPointF(x + 36, yy + 15), sy)
            # sparkline
            sp = r_["spark"]
            if len(sp) > 2:
                sx0, sw_, sh_ = x + w * 0.46, w * 0.22, 14
                lo, hi = min(sp), max(sp)
                rng_ = (hi - lo) or 1
                pts = [QPointF(sx0 + sw_ * k / (len(sp) - 1), yy + 6 + sh_ * (1 - (v - lo) / rng_)) for k, v in enumerate(sp)]
                p.setPen(QPen(qcol(colr, 220), 1.3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolyline(QPolygonF(pts))
            pr = _text_sprite(r_["price"], fp, qcol(C.WHITE, 240), dpr)
            p.drawPixmap(QPointF(x + w - 12 - pr.width() / dpr, yy + 1), pr)
            ch = _text_sprite(f"{'+' if up else ''}{r_['chg']:.2f}%", fs, qcol(colr, 230), dpr)
            p.drawPixmap(QPointF(x + w - 12 - ch.width() / dpr, yy + 15), ch)
            yy += 30
        return y + h

    def _paint_gauges(self, p: QPainter, x: float, y: float, w: float) -> float:
        """CPU / RAM / battery rings (real values, psutil)."""
        h = 92.0
        self._glass(p, QRectF(x, y, w, h))
        self._panel_label(p, x + 12, y + 14, "SYSTEM", time.strftime("%H:%M"), w - 24)
        st = self._stats
        items = [("CPU", st.get("cpu", 0)), ("RAM", st.get("ram", 0))]
        if st.get("bat") is not None:
            items.append(("PWR", st["bat"]))
        n = len(items)
        dpr = self.devicePixelRatioF()
        for k, (lab, val) in enumerate(items):
            gx = x + w * (k + 0.5) / n
            gy = y + 55
            rr = 21.0
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qcol("#9fc0ff", 40), 3.2))
            p.drawEllipse(QPointF(gx, gy), rr, rr)
            hot = val > 85 and lab != "PWR" or (lab == "PWR" and val < 20)
            pen = QPen(qcol("#ff8aa0" if hot else "#7fd6ff", 240), 3.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(gx - rr, gy - rr, rr * 2, rr * 2), 90 * 16, -int(360 * 16 * max(0.0, min(1.0, val / 100))))
            v = _text_sprite(f"{int(round(val))}%", _font(8, QFont.Weight.Bold, mono=True), qcol(C.WHITE, 240), dpr)
            p.drawPixmap(QPointF(gx - v.width() / dpr / 2, gy - 3 - v.height() / dpr / 2), v)
            l_ = _text_sprite(lab, _font(5.8, QFont.Weight.DemiBold, mono=True, spacing=1.0), qcol(C.TEXT_MED, 210), dpr)
            p.drawPixmap(QPointF(gx - l_.width() / dpr / 2, gy + 7 - l_.height() / dpr / 2), l_)
        return y + h

    def _paint_chat_cards(self, p: QPainter, x: float, top: float, bottom: float, w: float):
        """Right column: the last few things said, as glass cards stacked from the bottom."""
        items = [(t0, k, txt) for (t0, k, txt) in self._logs if k in ("you", "brinko")][-5:]
        if not items:
            return
        f = _font(8.2)
        fm = QFontMetricsF(f)
        y = bottom
        now = time.time()
        for t0, kind, txt in reversed(items):
            lines = []
            words, cur = txt.split(), ""
            for wd in words:
                trial = (cur + " " + wd).strip()
                if fm.horizontalAdvance(trial) > w - 24 and cur:
                    lines.append(cur)
                    cur = wd
                    if len(lines) == 2:
                        break
                else:
                    cur = trial
            if len(lines) < 3 and cur:
                lines.append(cur)
            lines = lines[:3]
            if len(lines) == 3 or (len(lines) == 2 and fm.horizontalAdvance(" ".join(words)) > 2 * (w - 24)):
                lines[-1] = fm.elidedText(lines[-1] + " …", Qt.TextElideMode.ElideRight, w - 24)
            h = 24 + 14 * len(lines)
            if y - h < top:
                break
            y -= h
            fade = min(1.0, (now - t0) / 0.25)
            self._glass(p, QRectF(x, y, w, h), fade * (1.0 if kind == "you" else 0.85), 10)
            self._panel_label(p, x + 10, y + 11, "YOU" if kind == "you" else "BRINKO",
                              time.strftime("%H:%M", time.localtime(t0)), w - 20,
                              "#ffffff" if kind == "you" else "#7fd6ff")
            p.setFont(f)
            p.setPen(qcol(C.WHITE, int(230 * fade)))
            for li, ln in enumerate(lines):
                p.drawText(QPointF(x + 12, y + 32 + 14 * li), ln)
            y -= 8

    def _paint_voice_bar(self, p: QPainter, W: float, H: float, acc: QColor):
        """Bottom centre: VOICE CHANNEL card with a live waveform (reference look)."""
        w = min(400.0, W * 0.42)
        h = 48.0
        x = W / 2 - w / 2
        y = H - self.bottom_inset - h - 10
        self._glass(p, QRectF(x, y, w, h))
        label, _ = _STATE_STYLE.get("MUTED" if self.muted else ("SPEAKING" if self.speaking else self.state),
                                    (self.state.title(), C.PRI))
        self._panel_label(p, x + 12, y + 13, f"VOICE CHANNEL  ·  {self.voice_name.upper()}  ·  24 KHZ",
                          label.upper(), w - 24, _mix(acc, QColor("#ffffff"), 0.2).name())
        hist = self._wave
        n = len(hist)
        if not n:
            return
        mid = y + 32
        span = w - 32
        p.setPen(Qt.PenStyle.NoPen)
        for k, v in enumerate(hist):
            bx = x + 16 + span * k / (n - 1)
            amp = 1.2 + 9.5 * v * (0.65 + 0.35 * math.sin(k * 1.7 + self._t * 9))
            a = int(90 + 150 * min(1.0, v * 2.5))
            p.setBrush(qcol("#bfe6ff" if v > 0.04 else "#6f8fd8", a))
            p.drawRect(QRectF(bx - 0.8, mid - amp, 1.6, amp * 2))

    def _paint_logs(self, p: QPainter, W, H, acc: QColor, top: float = 78.0):
        """Left column: recent activity in tiny mono text, newest at the bottom, typing in."""
        if W < 820:
            return
        f = _font(7.6, QFont.Weight.Normal, mono=True)
        fm = QFontMetricsF(f)
        x = 22.0
        bottom = H - self.bottom_inset - 26
        if bottom - top < 60:
            return
        lh = 14.0
        n = max(0, int((bottom - top) / lh))
        lines = self._logs[-n:] if n else []
        colw = min(290.0, W * 0.24) - 12
        p.setFont(f)
        p.setPen(qcol(C.TEXT_MED, 150))
        p.drawText(QPointF(x, top - 8), "ACTIVITY")
        p.setPen(QPen(qcol(acc.name(), 60), 1))
        p.drawLine(QPointF(x, top - 2), QPointF(x + colw, top - 2))
        now = time.time()
        y = bottom - lh * (len(lines) - 1)
        for i, (t0, kind, text) in enumerate(lines):
            age = len(lines) - 1 - i
            alpha = max(45, 235 - age * 10)
            colr = {"you": C.WHITE, "brinko": acc.name(), "file": C.GREEN}.get(kind, C.TEXT_MED)
            stamp = time.strftime("%H:%M:%S", time.localtime(t0))
            tag = {"you": "YOU ", "brinko": "BRK ", "file": "FILE"}.get(kind, "SYS ")
            body = text
            if i == len(lines) - 1:                          # newest line types in
                body = body[:int((now - t0) * 90)]
            s_ = fm.elidedText(f"{stamp}  {tag}  {body}", Qt.TextElideMode.ElideRight, colw)
            p.setPen(qcol(colr, alpha))
            p.drawText(QPointF(x, y), s_)
            y += lh

    def _paint_top_bar(self, p: QPainter, W: int):
        # gear
        gr = self._gear_rect()
        gc = gr.center()
        hov = gr.contains(self._hover_pt)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol(C.ACC2, 40 if hov else 18))
        p.drawRoundedRect(gr, 10, 10)
        gear_col = qcol(C.WHITE if hov else C.TEXT_MED, 230)
        pen = QPen(gear_col, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        teeth = []
        for i in range(8):
            a = math.radians(i * 45 + 22.5)
            teeth.append(QLineF(gc.x() + 6.5 * math.cos(a), gc.y() + 6.5 * math.sin(a),
                                gc.x() + 9.0 * math.cos(a), gc.y() + 9.0 * math.sin(a)))
        p.drawLines(teeth)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(gc, 5.8, 5.8)
        p.drawEllipse(gc, 2.2, 2.2)

        # wordmark
        dpr = self.devicePixelRatioF()
        wm = _text_sprite("B . R . I . N . K . O", _font(11, QFont.Weight.DemiBold, spacing=3.0), qcol(C.WHITE, 235), dpr)
        p.drawPixmap(QPointF(62, 32 - wm.height() / dpr / 2), wm)

        # status (centre): thin line + label, no heavy pill
        label, col = _STATE_STYLE.get("MUTED" if self.muted else
                                      ("SPEAKING" if self.speaking else self.state),
                                      (self.state.title(), C.PRI))
        if self.ultra and not self.muted:
            label += "  \u00b7  Ultra"
        col = self._acc.name()
        f = _font(8, QFont.Weight.DemiBold, mono=True, spacing=2.4)
        lab = _text_sprite(label.upper(), f, qcol(C.WHITE, 225), dpr)
        lw = lab.width() / dpr
        self._pill_w = lw if self._pill_w <= 0 else self._pill_w + (lw - self._pill_w) * 0.25
        yy = 32
        dot_a = int(150 + 105 * (0.5 + 0.5 * math.sin(self._t * 3)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol(col, dot_a))
        p.drawEllipse(QPointF(W / 2 - lw / 2 - 12, yy), 3.0, 3.0)
        p.drawPixmap(QPointF(W / 2 - lw / 2, yy - lab.height() / dpr / 2), lab)
        p.setPen(QPen(qcol(col, 70), 1))
        p.drawLine(QPointF(W / 2 - lw / 2 - 60, yy), QPointF(W / 2 - lw / 2 - 22, yy))
        p.drawLine(QPointF(W / 2 + lw / 2 + 10, yy), QPointF(W / 2 + lw / 2 + 48, yy))

        # clock (right) — cached text sprites; they only change once a minute
        clk = _text_sprite(time.strftime("%H:%M"), _font(15, QFont.Weight.DemiBold, mono=True),
                           qcol(C.WHITE, 235), dpr)
        p.drawPixmap(QPointF(W - 16 - clk.width() / dpr, 24 - clk.height() / dpr / 2), clk)
        day = _text_sprite(time.strftime("%a %d %b").upper(), _font(7.5, QFont.Weight.DemiBold, spacing=1.2),
                           qcol(C.TEXT_MED, 180), dpr)
        p.drawPixmap(QPointF(W - 16 - day.width() / dpr, 42 - day.height() / dpr / 2), day)

    def _paint_jobs(self, p: QPainter, W: int, y: float = 64.0, cw: float | None = None) -> float:
        jobs = self._visible_jobs()
        if not jobs:
            return y
        now = time.time()
        cw  = cw or min(280.0, W * 0.30)
        x   = W - cw - 16
        f_top = _font(7, QFont.Weight.DemiBold, mono=True, spacing=1.0)
        f_sum = _font(9, QFont.Weight.Normal)
        fm_sum = QFontMetricsF(f_sum)

        p.setFont(f_top)
        p.setPen(qcol(C.TEXT_MED, 170))
        running = sum(1 for j in jobs if j.get("status") == "running")
        p.drawText(QRectF(x, y, cw, 14), Qt.AlignmentFlag.AlignLeft,
                   f"BACKGROUND  ·  {running} RUNNING" if running else "BACKGROUND  ·  DONE")
        y += 20

        for j in jobs:
            status = j.get("status", "running")
            fade = 1.0
            if status != "running":
                age = now - self._finished_at.get(j["id"], now)
                fade = max(0.0, min(1.0, (8.0 - age) / 1.5))
            persona = persona_for(j["tool"])
            col = AGENT_COLORS.get(persona, C.PRI)
            ch  = 50.0
            r   = QRectF(x, y, cw, ch)
            self._glass(p, r, fade)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcol(col, int(255 * fade)))
            p.drawRoundedRect(QRectF(x + 8, y + 12, 3, ch - 24), 1.5, 1.5)

            elapsed = j.get("elapsed", 0.0)
            if status == "running":
                elapsed += now - self._jobs_rx_t
            secs = int(elapsed)
            el_txt = f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"

            p.setFont(f_top)
            p.setPen(qcol(col, int(230 * fade)))
            p.drawText(QRectF(x + 20, y + 8, cw - 80, 14),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{persona}  ·  {j['tool'].upper()}")
            p.setFont(f_sum)
            p.setPen(qcol(C.WHITE, int(235 * fade)))
            p.drawText(QRectF(x + 20, y + 24, cw - 64, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm_sum.elidedText(j.get("summary", ""), Qt.TextElideMode.ElideRight, cw - 70))

            ix, iy = x + cw - 24, y + ch / 2 + 4
            if status == "running":
                pen = QPen(qcol(col, 230), 2.0)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(QRectF(ix - 7, iy - 7, 14, 14), int(-((self._t * 360) % 360) * 16), 110 * 16)
            else:
                ok = status == "done"
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcol(C.GREEN if ok else C.RED, int(220 * fade)))
                p.drawEllipse(QPointF(ix, iy), 7, 7)
                pen = QPen(qcol(C.DARK, int(255 * fade)), 1.8)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                if ok:
                    p.drawPolyline(QPolygonF([QPointF(ix - 3, iy), QPointF(ix - 0.8, iy + 2.4),
                                              QPointF(ix + 3.2, iy - 2.6)]))
                else:
                    p.drawLine(QPointF(ix - 2.5, iy - 2.5), QPointF(ix + 2.5, iy + 2.5))
                    p.drawLine(QPointF(ix + 2.5, iy - 2.5), QPointF(ix - 2.5, iy + 2.5))
            p.setFont(f_top)
            p.setPen(qcol(C.TEXT_MED, int(200 * fade)))
            p.drawText(QRectF(x + cw - 74, y + 8, 64, 14),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, el_txt)
            y += ch + 8
        return y


_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(4, 13, 21, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 20px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(_font(font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("Welcome to BRINKO", 15, True, color=C.WHITE))
        layout.addWidget(_lbl("Add your free Gemini API key to get started.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(_font(10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #0a1824; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 10px; padding: 4px 12px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(8)

        layout.addWidget(_lbl("OPENROUTER API KEY  (optional backup)", 8, color=C.TEXT_DIM,
                       align=Qt.AlignmentFlag.AlignLeft))
        self._or_input = QLineEdit()
        self._or_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._or_input.setPlaceholderText("sk-or-…")
        self._or_input.setFont(_font(10))
        self._or_input.setFixedHeight(32)
        self._or_input.setStyleSheet(f"""
            QLineEdit {{
                background: #0a1824; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 10px; padding: 4px 12px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.ACC2}; }}
        """)
        layout.addWidget(self._or_input)

        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(_font(9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("Start BRINKO")
        init_btn.setFont(_font(10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C.ACC2}, stop:1 {C.ACC});
                color: white; border: none; border-radius: 12px;
            }}
            QPushButton:hover {{ background: {C.ACC2}; }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 10px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #0a1824; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 10px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        or_key = self._or_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, or_key, self._sel_os)


class SettingsPanel(QWidget):
    """
    Floating settings panel — tap the ⚙ icon on the HUD to open it.
    Lets the user set up email, API keys, and preferences without ever
    touching a JSON file.
    """
    closed = pyqtSignal()
    on_voice_changed = None          # set by main: restarts the voice session with the new voice

    _FIELD_MAP = [
        # (section_label, [(field_label, config_key, is_password, placeholder)])
        ("API KEYS", [
            ("Gemini API Key",    "gemini_api_key",     True,  "AIza..."),
            ("OpenRouter Key",    "openrouter_api_key", True,  "sk-or-..."),
            ("Bland.ai Key",      "bland_api_key",      True,  "optional — for phone calls"),
        ]),
        ("EMAIL", [
            ("Email Address",     "email_addr",         False, "you@gmail.com"),
            ("App Password",      "email_pass",         True,  "xxxx xxxx xxxx xxxx"),
        ]),
        ("PREFERENCES", [
            ("City",              "pref_city",          False, "e.g. Amsterdam"),
        ]),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SettingsPanel {{
                background: {C.PANEL};
                border: 1px solid rgba(63, 230, 255, 60);
                border-radius: 20px;
            }}
        """)
        self.setMinimumWidth(340)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        # header
        hdr = QHBoxLayout()
        title = QLabel("Settings")
        title.setFont(_font(14, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setFont(_font(11))
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 14px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; }}
        """)
        close_btn.clicked.connect(self._close)
        hdr.addWidget(close_btn)
        outer.addLayout(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 0;")
        outer.addWidget(sep)

        self._fields: dict[str, QLineEdit] = {}

        for section_label, fields in self._FIELD_MAP:
            sec_lbl = QLabel(section_label)
            sec_lbl.setFont(_font(7.5, QFont.Weight.Bold, spacing=1.5))
            sec_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; margin-top: 6px;")
            outer.addWidget(sec_lbl)

            for field_label, key, is_pass, placeholder in fields:
                row = QHBoxLayout(); row.setSpacing(8)
                lbl = QLabel(field_label)
                lbl.setFont(_font(9))
                lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
                lbl.setFixedWidth(110)
                row.addWidget(lbl)

                edit = QLineEdit()
                edit.setPlaceholderText(placeholder)
                edit.setFont(_font(9.5))
                if is_pass:
                    edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setStyleSheet(f"""
                    QLineEdit {{
                        background: {C.PANEL2}; color: {C.WHITE};
                        border: 1px solid {C.BORDER}; border-radius: 10px; padding: 7px 12px;
                    }}
                    QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
                """)
                row.addWidget(edit)
                outer.addLayout(row)
                self._fields[key] = edit

        # voice picker (saved as brinko_voice; BRINKO switches live)
        vrow = QHBoxLayout(); vrow.setSpacing(8)
        vl = QLabel("Voice"); vl.setFont(_font(9)); vl.setFixedWidth(110)
        vl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        vrow.addWidget(vl)
        from PyQt6.QtWidgets import QComboBox
        self._voice = QComboBox()
        self._voice.setFont(_font(9.5))
        for v, d in (("Sulafat", "warm"), ("Aoede", "breezy"), ("Achernar", "soft"), ("Despina", "smooth"),
                     ("Leda", "youthful"), ("Kore", "firm"), ("Puck", "male, friendly"), ("Charon", "male, deep"),
                     ("Fenrir", "male, strong"), ("Orus", "male, calm")):
            self._voice.addItem(f"{v}  ·  {d}", v)
        self._voice.setStyleSheet(f"""
            QComboBox {{ background: {C.PANEL2}; color: {C.WHITE}; border: 1px solid {C.BORDER};
                         border-radius: 10px; padding: 6px 12px; }}
            QComboBox:focus {{ border: 1px solid {C.PRI}; }}
            QComboBox QAbstractItemView {{ background: {C.PANEL}; color: {C.WHITE};
                         selection-background-color: {C.PRI_DIM}; border: 1px solid {C.BORDER_B}; }}
        """)
        vrow.addWidget(self._voice)
        outer.addLayout(vrow)

        # show-password toggle
        show_pw = QPushButton("👁  Show passwords")
        show_pw.setFont(_font(8))
        show_pw.setCheckable(True)
        show_pw.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: none; text-align: left;
            }}
            QPushButton:checked {{ color: {C.TEXT_MED}; }}
        """)
        def _toggle_pw(checked):
            mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            for k, e in self._fields.items():
                if k in ("gemini_api_key", "openrouter_api_key", "bland_api_key", "email_pass"):
                    e.setEchoMode(mode)
        show_pw.toggled.connect(_toggle_pw)
        outer.addWidget(show_pw)

        phone_btn = QPushButton("📱  Connect phone")
        phone_btn.setFixedHeight(38)
        phone_btn.setFont(_font(10, QFont.Weight.Bold))
        phone_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        phone_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,255,255,10); color: {C.WHITE};
                border: 1px solid rgba(63,230,255,70); border-radius: 12px;
            }}
            QPushButton:hover {{ background: rgba(63,230,255,40); }}
        """)
        phone_btn.clicked.connect(self._open_phone_dialog)
        outer.addWidget(phone_btn)

        outer.addSpacing(4)

        save_btn = QPushButton("Save changes")
        save_btn.setFixedHeight(38)
        save_btn.setFont(_font(10, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C.ACC2}, stop:1 {C.ACC});
                color: white; border: none; border-radius: 12px;
            }}
            QPushButton:hover {{ background: {C.ACC2}; }}
        """)
        save_btn.clicked.connect(self._save)
        outer.addWidget(save_btn)

        self._status = QLabel("")
        self._status.setFont(_font(8))
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        outer.addWidget(self._status)

        self._load_existing()

    def _config_dir(self) -> Path:
        try:
            return Path(__file__).resolve().parent / "config"
        except Exception:
            return Path("config")

    def _load_existing(self):
        api_path   = self._config_dir() / "api_keys.json"
        email_path = self._config_dir() / "email_config.json"
        try:
            if api_path.exists():
                d = json.loads(api_path.read_text(encoding="utf-8"))
                self._fields["gemini_api_key"].setText(d.get("gemini_api_key", ""))
                self._fields["openrouter_api_key"].setText(d.get("openrouter_api_key", ""))
                self._fields["bland_api_key"].setText(d.get("bland_api_key", ""))
                self._fields["pref_city"].setText(d.get("user_city", ""))
                i = self._voice.findData(d.get("brinko_voice") or DEFAULT_VOICE)
                self._voice.setCurrentIndex(max(0, i))
        except Exception:
            pass
        try:
            if email_path.exists():
                d = json.loads(email_path.read_text(encoding="utf-8"))
                self._fields["email_addr"].setText(d.get("email", ""))
                self._fields["email_pass"].setText(d.get("app_password", ""))
        except Exception:
            pass

    def _save(self):
        cfg_dir = self._config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)

        # --- api_keys.json ---
        api_path = cfg_dir / "api_keys.json"
        try:
            existing = json.loads(api_path.read_text(encoding="utf-8")) if api_path.exists() else {}
        except Exception:
            existing = {}

        def _v(key): return self._fields[key].text().strip()

        if _v("gemini_api_key"):    existing["gemini_api_key"]     = _v("gemini_api_key")
        if _v("openrouter_api_key"): existing["openrouter_api_key"] = _v("openrouter_api_key")
        if _v("bland_api_key"):     existing["bland_api_key"]       = _v("bland_api_key")
        if _v("pref_city"):         existing["user_city"]            = _v("pref_city")
        old_voice = existing.get("brinko_voice") or DEFAULT_VOICE
        existing["brinko_voice"] = self._voice.currentData()
        if "os_system" not in existing:
            import platform; existing["os_system"] = platform.system().lower()

        try:
            api_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception as e:
            self._status.setText(f"⚠️ Could not save api_keys.json: {e}")
            self._status.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return

        # --- email_config.json ---
        if _v("email_addr") and _v("email_pass"):
            # Gmail/Google only accept a 16-letter APP password here. Refuse to
            # store anything else — it won't work, and it's probably the user's
            # real Google password, which must not sit in a plain-text file.
            addr = _v("email_addr").lower()
            pw   = _v("email_pass").replace(" ", "")
            if addr.endswith(("@gmail.com", "@googlemail.com")) and not (len(pw) == 16 and pw.isalpha()):
                self._fields["email_pass"].clear()
                self._status.setText("⚠️ That's not an app password (it must be 16 letters). Create one at "
                                     "myaccount.google.com/apppasswords — never use your normal Google password.")
                self._status.setWordWrap(True)
                self._status.setStyleSheet(f"color: {C.RED}; background: transparent;")
                return
            email_path = cfg_dir / "email_config.json"
            try:
                email_path.write_text(json.dumps({
                    "email":        _v("email_addr"),
                    "app_password": _v("email_pass"),
                }, indent=2), encoding="utf-8")
            except Exception as e:
                self._status.setText(f"⚠️ Could not save email_config.json: {e}")
                self._status.setStyleSheet(f"color: {C.RED}; background: transparent;")
                return

        self._status.setText("✓  Saved — email works right away; API key changes need a restart")
        self._status.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        if existing["brinko_voice"] != old_voice and self.on_voice_changed:
            self.on_voice_changed()

    def _open_phone_dialog(self):
        dlg = PhonePairDialog(self.window())
        dlg.show()

    def _close(self):
        self.hide()
        self.closed.emit()



class PhonePairDialog(QWidget):
    """'Connect phone': QR code with the pairing link + reachability status."""

    _approval_sig = pyqtSignal(str)
    _done_sig     = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle("Connect your phone to BRINKO")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(f"""
            QWidget {{ background: {C.PANEL}; color: {C.WHITE}; }}
            QLabel#h {{ font-size: 15pt; font-weight: 700; }}
            QLabel#dim {{ color: {C.TEXT_MED}; }}
            QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C.ACC2}, stop:1 {C.ACC});
                           color: white; border: none; border-radius: 12px; padding: 10px 16px; font-weight: 700; }}
            QPushButton#ghost {{ background: rgba(255,255,255,12); border: 1px solid rgba(63,230,255,70); }}
        """)
        self.setFixedWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(10)

        h = QLabel("Connect your phone"); h.setObjectName("h"); h.setFont(_font(15, QFont.Weight.Bold))
        lay.addWidget(h)
        self._sub = QLabel("Checking connection options…"); self._sub.setObjectName("dim")
        self._sub.setWordWrap(True); self._sub.setFont(_font(9.5))
        lay.addWidget(self._sub)

        self._qr = QLabel(); self._qr.setAlignment(Qt.AlignmentFlag.AlignCenter); self._qr.setMinimumHeight(280)
        lay.addWidget(self._qr)
        self._url = QLabel(""); self._url.setObjectName("dim"); self._url.setFont(_font(8.5, mono=True))
        self._url.setWordWrap(True); self._url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url)

        self._steps = QLabel(""); self._steps.setWordWrap(True); self._steps.setFont(_font(9.5))
        self._steps.setTextFormat(Qt.TextFormat.RichText); self._steps.setOpenExternalLinks(True)
        lay.addWidget(self._steps)

        row = QHBoxLayout()
        self._anywhere = QPushButton("Turn on access from anywhere")
        self._anywhere.clicked.connect(self._enable_anywhere)
        self._anywhere.hide()
        row.addWidget(self._anywhere)
        close = QPushButton("Done"); close.setObjectName("ghost"); close.clicked.connect(self.close)
        row.addWidget(close)
        lay.addLayout(row)
        self._approval_sig.connect(self._on_approval_url)
        self._done_sig.connect(self._on_enable_done)
        QTimer.singleShot(50, self._refresh)

    def _refresh(self):
        try:
            from remote_server import pairing_info
            info = pairing_info()
        except Exception as e:
            self._sub.setText(f"Phone link unavailable: {e}")
            return
        self._show_qr(info["url"])
        self._url.setText(info["url"].split("?pair=")[0] + "  (pairing code included in the QR)")
        ts = info["tailscale"]
        if info["mode"] == "anywhere":
            self._sub.setText("✓ Works anywhere — at home, on mobile data, on other Wi-Fi.")
            self._steps.setText("1. Scan the QR code with your phone's camera.<br>"
                                "2. In Chrome tap <b>⋮ → Install app</b>.<br>"
                                "Tailscale must stay installed and on on both devices.")
            self._anywhere.hide()
        elif ts["installed"]:
            self._sub.setText("Home Wi-Fi only right now. Tailscale is installed — one click makes it work "
                              "anywhere (and enables the microphone in the app).")
            self._steps.setText("Click <b>Turn on access from anywhere</b>, then scan the new QR code.")
            self._anywhere.show()
        else:
            self._sub.setText("Home Wi-Fi only, typing only (Chrome blocks the mic on plain home links).")
            self._steps.setText(
                "<b>To use it anywhere — with voice:</b><br>"
                "1. Install Tailscale on this PC: <a style='color:#8ff7ff' href='https://tailscale.com/download/windows'>"
                "tailscale.com/download</a> and sign in.<br>"
                "2. Install <b>Tailscale</b> from the Play Store on your phone, same account.<br>"
                "3. Reopen this window and click <b>Turn on access from anywhere</b>.")
            self._anywhere.hide()
        self.adjustSize()

    def _show_qr(self, url: str):
        try:
            import io
            import qrcode
            qr = qrcode.QRCode(border=2, box_size=8)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#031a24", back_color="#e9fbff").convert("RGB")
            buf = io.BytesIO(); img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._qr.setPixmap(px.scaled(270, 270, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.FastTransformation))
        except Exception as e:
            self._qr.setText(f"(QR unavailable: {e})")

    def _enable_anywhere(self):
        """Runs `tailscale serve` on a worker thread (it can wait minutes for
        the one-time approval in the browser — doing that on the GUI thread
        froze the whole app)."""
        from remote_server import enable_tailscale_serve
        self._anywhere.setEnabled(False)
        self._anywhere.setText("Turning on…")
        self._steps.setText("Talking to Tailscale…")

        def work():
            ok, msg = enable_tailscale_serve(on_approval_url=self._approval_sig.emit)
            self._done_sig.emit(ok, msg)
        threading.Thread(target=work, daemon=True, name="TailscaleServe").start()

    def _on_approval_url(self, url: str):
        """First time only: Tailscale needs you to allow 'Serve' for your network."""
        import webbrowser
        webbrowser.open(url)
        self._anywhere.setText("Waiting for you to click Enable…")
        self._steps.setText(
            "<b>One-time step:</b> a Tailscale page just opened in your browser.<br>"
            "Click <b>Enable</b> there (it turns on secure HTTPS links for your own devices).<br>"
            "BRINKO finishes automatically once you've enabled it — keep this window open.<br>"
            f"<span style='color:#6fb3c7'>Didn't open? <a style='color:#8ff7ff' href='{html.escape(url)}'>Open the page</a></span>")

    def _on_enable_done(self, ok: bool, msg: str):
        self._anywhere.setEnabled(True)
        self._anywhere.setText("Turn on access from anywhere")
        if ok:
            self._refresh()
            self._sub.setText("✓ Done — works anywhere now, with voice. Scan the new QR code.")
            return
        self._steps.setText(f"<span style='color:#ff3355'>Didn't work yet:</span> {html.escape(msg)[:400]}<br>"
                            "Click the button again to retry.")


class MiniOrb(QWidget):
    """Floating JARVIS orb shown while the main window is minimized: always on top,
    pulses with the voices, state colour; click = bring BRINKO back, drag = move,
    right-click = menu."""
    SIZE = 132

    def __init__(self, hud: "HudCanvas", restore_cb, mute_cb):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hud, self._restore, self._mute = hud, restore_cb, mute_cb
        self._press = None
        self._moved = False
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.SIZE - 24, scr.bottom() - self.SIZE - 24)

    def paintEvent(self, _):
        h = self._hud
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.SIZE / 2
        R = self.SIZE * 0.185 * (1.0 + 0.04 * h._level)     # glow (5.2 R) must fit inside the window
        h._paint_sphere(p, c, c, R, h._acc, h._level, h._energy, h._t)
        # orbiting dots
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(10):
            a = h._t * (0.6 + 0.04 * i) + i * 0.63
            rr = R * (1.35 + 0.18 * (i % 3)) + 6 * h._warp * (i % 2)
            p.setBrush(qcol(h._acc.name(), 120 + 10 * i))
            p.drawEllipse(QPointF(c + rr * math.cos(a), c + rr * math.sin(a) * 0.55), 1.4, 1.4)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.globalPosition().toPoint() - self.pos()
            self._moved = False
        elif e.button() == Qt.MouseButton.RightButton:
            from PyQt6.QtWidgets import QMenu
            m = QMenu(self)
            m.setStyleSheet(f"QMenu {{ background: {C.PANEL}; color: {C.WHITE}; border: 1px solid {C.BORDER_B}; }}"
                            f"QMenu::item:selected {{ background: {C.PRI_DIM}; }}")
            m.addAction("Open BRINKO", self._restore)
            m.addAction("Mute / unmute mic", self._mute)
            m.exec(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self.move(e.globalPosition().toPoint() - self._press)
            self._moved = True

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press is not None and not self._moved:
            self._restore()
        self._press = None


class ChatOverlay(QFrame):
    """
    Floating chat card at the bottom of the HUD. Shows only You:/Brinko:
    lines (SYS/FILE lines are filtered out) — the last 3, newest at the
    bottom, older ones fading away — plus the mic toggle and text input.
    """
    send_requested = pyqtSignal(str)

    _MAX_LINES = 3
    CARD_H     = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(self.CARD_H)
        self.setStyleSheet("""
            #chatCard {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(44, 82, 214, 150), stop:1 rgba(20, 44, 160, 140));
                border: 1px solid rgba(143, 180, 255, 85);
                border-radius: 16px;
            }
        """)

        self.log = _ChatLogShim(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 12, 12, 12)
        lay.setSpacing(3)

        self._lines: list[QLabel] = []
        self._raw_lines: list[str] = []
        for i in range(self._MAX_LINES):
            lbl = QLabel("")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setFont(_font(9.5 if i == self._MAX_LINES - 1 else 9))
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            lbl.setWordWrap(True)
            lbl.setMinimumHeight(18)
            lbl.setMaximumHeight(34)
            lbl.setStyleSheet("background: transparent;")
            lay.addWidget(lbl)
            self._lines.append(lbl)

        self._placeholder = "Say <b>“Hey Jarvis”</b> or type below — I can keep working in the background while you do your thing."
        self._lines[-1].setText(f"<span style='color: rgba(200,240,255,0.45)'>{self._placeholder}</span>")

        lay.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.mute_btn = QPushButton("🎙")
        self.mute_btn.setFixedSize(38, 38)
        self.mute_btn.setFont(QFont("Segoe UI Emoji" if _OS == "Windows" else "Arial", 12))
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.mute_btn)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Message BRINKO…   (Space to focus · F4 mic · Ctrl+, settings)")
        self.input.setFont(_font(10))
        self.input.setFixedHeight(38)
        self.input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255, 255, 255, 10); color: {C.WHITE};
                border: 1px solid rgba(63, 230, 255, 40); border-radius: 19px;
                padding: 0 16px; selection-background-color: {C.PRI};
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; background: rgba(255, 255, 255, 16); }}
        """)
        row.addWidget(self.input, stretch=1)

        send = QPushButton("↑")
        send.setFixedSize(38, 38)
        send.setFont(_font(13, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setToolTip("Send (Enter)")
        send.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C.ACC2}, stop:1 {C.ACC});
                color: white; border: none; border-radius: 19px;
            }}
            QPushButton:hover {{ background: {C.ACC2}; }}
            QPushButton:pressed {{ background: {C.PRI}; }}
        """)
        row.addWidget(send)
        lay.addLayout(row)

        self.input.returnPressed.connect(self._emit)
        send.clicked.connect(self._emit)

    def push_exchange(self, text: str):
        tl = text.strip().lower()
        if not (tl.startswith("you:") or tl.startswith("brinko:")):
            return

        self._raw_lines.append(text.strip())
        self._raw_lines = self._raw_lines[-self._MAX_LINES:]
        self._type_pos = 0 if tl.startswith("brinko:") else None
        if self._type_pos is not None:
            if not hasattr(self, "_type_timer"):
                self._type_timer = QTimer(self)
                self._type_timer.timeout.connect(self._type_step)
            self._type_timer.start(16)
        self._render_lines()

    def _type_step(self):
        """Typewriter reveal for BRINKO's newest line (~180 chars/sec)."""
        if self._type_pos is None or not self._raw_lines:
            self._type_timer.stop()
            return
        full = len(self._raw_lines[-1]) - 6
        self._type_pos += 3
        if self._type_pos >= full:
            self._type_pos = None
            self._type_timer.stop()
        self._render_lines()

    def _render_lines(self):
        padded = ([""] * (self._MAX_LINES - len(self._raw_lines))) + self._raw_lines
        opacities = [0.32, 0.62, 1.0]

        for i, (lbl, raw) in enumerate(zip(self._lines, padded)):
            if not raw:
                lbl.setText("")
                continue
            a     = opacities[i]
            is_ai = raw.lower().startswith("brinko:")
            plain = raw[6:].strip() if is_ai else raw[4:].strip()
            if i == self._MAX_LINES - 1 and is_ai and getattr(self, "_type_pos", None) is not None:
                plain = plain[:self._type_pos] + "▍"
            body  = html.escape(plain)
            if is_ai:
                tag = f"<span style='color: rgba(63,230,255,{a}); font-weight:700; letter-spacing:1px; font-size:8pt'>BRINKO</span>"
                txt = f"<span style='color: rgba(232,250,255,{a})'>{body}</span>"
            else:
                tag = f"<span style='color: rgba(140,205,255,{a}); font-weight:700; letter-spacing:1px; font-size:8pt'>YOU</span>"
                txt = f"<span style='color: rgba(205,232,242,{a})'>{body}</span>"
            lbl.setText(f"{tag}&nbsp;&nbsp;&nbsp;{txt}")

    def _emit(self):
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.send_requested.emit(text)


class _ChatLogShim:
    """Routes You:/Brinko: lines to the chat card; drops SYS/FILE noise."""
    def __init__(self, overlay: "ChatOverlay"):
        self._overlay = overlay

    def append_log(self, text: str):
        self._overlay.push_exchange(text)


class MainWindow(QMainWindow):
    _log_sig   = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _agent_sig = pyqtSignal(str)
    _ultra_sig = pyqtSignal(bool)
    _jobs_sig  = pyqtSignal(list)
    _mute_sig  = pyqtSignal(bool)
    _settings_sig = pyqtSignal()

    def __init__(self, face_path: str):
        super().__init__()
        self.setWindowTitle("BRINKO")
        _ico = BASE_DIR / "assets" / "brinko.ico"
        if _ico.exists():
            from PyQt6.QtGui import QIcon
            self.setWindowIcon(QIcon(str(_ico)))
        self.setMinimumSize(_MIN_W, _MIN_H)

        screen = QApplication.primaryScreen().availableGeometry()
        w = min(_DEFAULT_W, int(screen.width() * 0.9))
        h = min(_DEFAULT_H, int(screen.height() * 0.9))
        self.resize(w, h)
        self.move(screen.x() + (screen.width() - w) // 2, screen.y() + (screen.height() - h) // 2)

        self.on_text_command  = None
        self._muted            = False
        self._current_file: str | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.hud = HudCanvas(face_path)
        root.addWidget(self.hud, stretch=1)

        # chat card floats over the bottom of the HUD
        self._chat_overlay = ChatOverlay(central)
        self._chat_overlay.mute_btn.clicked.connect(self._toggle_mute)
        self._chat_overlay.send_requested.connect(self._send_overlay_text)
        self._mute_btn = self._chat_overlay.mute_btn
        self._log      = self._chat_overlay.log
        self._input    = self._chat_overlay.input
        self._style_mute_btn()

        self._settings_panel = SettingsPanel(central)
        self._settings_panel.hide()
        self._settings_panel.closed.connect(lambda: self._settings_panel.hide())

        self.on_orb_tap = None
        self.hud.orb_tapped.connect(self._on_orb_tapped)
        self.hud.file_dropped.connect(self._on_file_selected)
        self.hud.settings_tapped.connect(self._toggle_settings_panel)

        self._log_sig.connect(self._log.append_log)
        self._log_sig.connect(self.hud.push_log)
        self._state_sig.connect(self._apply_state)
        self._agent_sig.connect(self.hud.flash_agent)
        self._ultra_sig.connect(self.hud.set_ultra)
        self._jobs_sig.connect(self.hud.set_jobs)
        self._mute_sig.connect(self._set_muted)
        self._settings_sig.connect(self._toggle_settings_panel)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        self._mini = MiniOrb(self.hud, self._restore_from_mini, self._toggle_mute)
        self.hud.mini = self._mini

        QShortcut(QKeySequence("F4"), self).activated.connect(self._toggle_mute)
        QShortcut(QKeySequence("F11"), self).activated.connect(self._toggle_fullscreen)
        QShortcut(QKeySequence("Space"), self).activated.connect(self._input.setFocus)
        QShortcut(QKeySequence("Ctrl+,"), self).activated.connect(self._toggle_settings_panel)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._on_escape)

    def changeEvent(self, e):
        from PyQt6.QtCore import QEvent
        if e.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._mini.show()
                self.hud._wake()
            else:
                self._mini.hide()
        super().changeEvent(e)

    def _restore_from_mini(self):
        self._mini.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_orb_tapped(self):
        if self.on_orb_tap:
            try:
                self.on_orb_tap()
            except Exception as e:
                print(f"[UI] orb tap handler: {e}")
        self._input.setFocus()

    def _on_escape(self):
        if self._settings_panel.isVisible():
            self._settings_panel.hide()
        elif self._input.hasFocus():
            self._input.clearFocus()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _position_chat(self):
        cw = self.centralWidget()
        w  = min(820, cw.width() - 32)
        h  = ChatOverlay.CARD_H
        self._chat_overlay.setGeometry((cw.width() - w) // 2, cw.height() - h - 16, w, h)
        self._chat_overlay.raise_()
        self.hud.bottom_inset = h + 16

    def _position_settings_panel(self):
        cw = self.centralWidget()
        w  = 400
        h  = min(max(self._settings_panel.sizeHint().height() + 20, 440), cw.height() - 80)
        self._settings_panel.setGeometry(16, 60, w, h)

    def _toggle_settings_panel(self):
        if self._settings_panel.isVisible():
            self._settings_panel.hide()
        else:
            self._position_settings_panel()
            self._settings_panel.show()
            self._settings_panel.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_chat()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_chat()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 430
            cw = self.centralWidget()
            self._overlay.setGeometry((cw.width() - ow) // 2, (cw.height() - oh) // 2, ow, oh)
            self._overlay.raise_()
        if self._settings_panel.isVisible():
            self._position_settings_panel()

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size = _fmt_size(p.stat().st_size)
        except OSError:
            size = "?"
        self._log.append_log(f"FILE: {icon} {p.name} ({size}) loaded")
        self._input.setFocus()
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _set_muted(self, v: bool):
        if bool(v) != self._muted:
            self._toggle_mute()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")
        self.hud._wake()

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇")
            self._mute_btn.setToolTip("Mic off — typing still works. Click to unmute (F4)")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(0, 255, 170, 22); color: {C.MUTED_C};
                    border: 1px solid rgba(0, 255, 170, 120); border-radius: 19px;
                }}
                QPushButton:hover {{ background: rgba(0, 255, 170, 40); }}
            """)
        else:
            self._mute_btn.setText("🎙")
            self._mute_btn.setToolTip("Mic on — click to mute (F4)")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 255, 255, 10); color: {C.WHITE};
                    border: 1px solid rgba(63, 230, 255, 60); border-radius: 19px;
                }}
                QPushButton:hover {{ background: rgba(63, 230, 255, 40); }}
            """)

    def _send_overlay_text(self, txt: str):
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        if self.hud.state == state and self.hud.speaking == (state == "SPEAKING"):
            return
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        self.hud._wake()

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 430
        ov.setGeometry((cw.width() - ow) // 2, (cw.height() - oh) // 2, ow, oh)
        ov.done.connect(self._on_setup_done)
        ov.show()
        ov.raise_()
        self._overlay = ov

    def _on_setup_done(self, key: str, or_key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({
                "gemini_api_key":    key,
                "openrouter_api_key": or_key,
                "os_system":         os_name,
            }, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. BRINKO online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class BrinkoUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)
        self._log_subscribers: list = []
        self._log_sub_lock = threading.Lock()
        # visual effects host (screensaver, camera powers, document builder)
        try:
            from fx_window import FxHost
            import remote_server
            self.fx = FxHost(lambda page, query: remote_server.fx_url(page, query))
        except Exception as e:
            print(f"[UI] FX host unavailable: {e}")
            self.fx = None

    def subscribe_log(self, fn):
        """Registers fn(text) to be called on every write_log — used by
        remote_server.py to mirror the activity log to a connected phone.
        Safe to call from any thread."""
        with self._log_sub_lock:
            self._log_subscribers.append(fn)

    def unsubscribe_log(self, fn):
        with self._log_sub_lock:
            if fn in self._log_subscribers:
                self._log_subscribers.remove(fn)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        # Qt widgets may only be touched on the GUI thread — go via a signal.
        self._win._mute_sig.emit(bool(v))

    @property
    def current_file(self) -> str | None:
        return self._win._current_file

    @property
    def on_orb_tap(self):
        return self._win.on_orb_tap

    @on_orb_tap.setter
    def on_orb_tap(self, cb):
        self._win.on_orb_tap = cb

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def flash_agent(self, name: str):
        self._win._agent_sig.emit(name)

    def set_ultra(self, on: bool):
        self._win._ultra_sig.emit(bool(on))

    def set_jobs(self, jobs: list):
        """Background-job snapshot from agent.background_jobs — any thread."""
        self._win._jobs_sig.emit(list(jobs))

    def set_mic_level(self, level: float):
        # plain float, read by the HUD's own animation tick
        self._win.hud.set_mic_level(level)

    def set_out_level(self, level: float):
        self._win.hud.set_out_level(level)

    def open_settings(self):
        """Opens the floating settings panel — callable from any thread."""
        self._win._settings_sig.emit()

    def roll_call_sweep(self, names: list[str] | None = None, interval: float = 0.28):
        """Lights up each agent node in sequence — call from any thread."""
        for n in (names or AGENT_RING):
            self.flash_agent(n)
            time.sleep(interval)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)
        with self._log_sub_lock:
            subs = list(self._log_subscribers)
        for fn in subs:
            try:
                fn(text)
            except Exception as e:
                print(f"[BrinkoUI] ⚠️ log subscriber error: {e}")

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

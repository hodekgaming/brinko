# actions/voice_focus.py
#
# Keeps BRINKO focused on YOUR voice instead of YouTube / Spotify / games.
#
# A single microphone can't tell a person in the room from a person in a
# video, so BRINKO uses what Windows *does* know: which apps are currently
# playing sound (per-app peak meters via pycaw).
#
#   • Nothing else playing  → hands-free: the mic streams to Gemini as usual.
#   • Another app playing   → STANDBY: mic audio is NOT sent, so video/music
#                             speech can't trigger BRINKO.
#   • You activate BRINKO (Ctrl+Alt+B anywhere or tap the orb)
#                           → other apps are turned down to ~15% ("ducked"),
#                             the mic opens, and you talk normally. While you
#                             or BRINKO keep talking it stays open; after a few
#                             quiet seconds the volume comes back and it goes
#                             back to standby.
#
# All pycaw/COM calls happen on this module's own thread.

from __future__ import annotations

import os
import threading
import time

HOTKEY_LABEL       = "Ctrl+Alt+B"
_MOD_ALT, _MOD_CONTROL, _MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
_VK_B              = 0x42

MEDIA_PEAK         = 0.015   # per-app peak above this = "playing"
MEDIA_HOLD_SECS    = 2.5     # keep "media playing" this long after it goes quiet (pauses between lines)
ACTIVE_WINDOW_SECS = 9.0     # mic stays open this long after the last activity
DUCK_TO            = 0.15    # other apps' volume while you talk to BRINKO


class VoiceFocus:
    def __init__(self, ui=None):
        self.ui = ui
        self.enabled = True
        self.media_playing = False
        self._media_seen_at = 0.0
        self._active_until = 0.0
        self._want_duck = False
        self._ducked: dict[int, tuple[object, float, float]] = {}   # pid → (volume_iface, original, set_to)
        self._lock = threading.Lock()
        self._listeners: list = []
        self._last_gated: bool | None = None
        self._available = True
        threading.Thread(target=self._loop, daemon=True, name="VoiceFocus").start()
        threading.Thread(target=self._hotkey_loop, daemon=True, name="VoiceFocusHotkey").start()

    # ── public API ──────────────────────────────────────────────────────
    def on_change(self, fn):
        """fn(gated: bool) — called when standby turns on/off."""
        self._listeners.append(fn)

    def mic_open(self) -> bool:
        """Should mic audio be sent to Gemini right now? (cheap; safe from the audio callback)"""
        if not self.enabled or not self._available:
            return True
        return time.time() < self._active_until or not self.media_playing

    @property
    def gated(self) -> bool:
        return not self.mic_open()

    @property
    def active(self) -> bool:
        return time.time() < self._active_until

    def activate(self, reason: str = ""):
        """User wants to talk: open the mic and duck other audio."""
        with self._lock:
            self._active_until = time.time() + ACTIVE_WINDOW_SECS
            self._want_duck = self.media_playing
        print(f"[VoiceFocus] 🎙 activated ({reason or 'manual'})")
        if self.ui:
            try:
                self.ui.write_log(f"SYS: Listening ({reason or 'activated'}).")
            except Exception:
                pass
        self._notify()

    def keep_alive(self, secs: float = ACTIVE_WINDOW_SECS):
        """Called while the user or BRINKO is talking — only extends an already-open window."""
        with self._lock:
            if time.time() < self._active_until:
                self._active_until = max(self._active_until, time.time() + secs)

    # ── internals ───────────────────────────────────────────────────────
    def _notify(self):
        g = self.gated
        if g != self._last_gated:
            self._last_gated = g
            for fn in list(self._listeners):
                try:
                    fn(g)
                except Exception as e:
                    print(f"[VoiceFocus] listener error: {e}")

    def _loop(self):
        try:
            import comtypes
            comtypes.CoInitialize()
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        except Exception as e:
            print(f"[VoiceFocus] ⚠️ per-app audio meters unavailable ({e}) — always hands-free.")
            self._available = False
            return

        my_pid = os.getpid()
        while True:
            try:
                sessions = AudioUtilities.GetAllSessions()
                loud = False
                for s in sessions:
                    proc = s.Process
                    if proc is None or proc.pid == my_pid:
                        continue
                    try:
                        peak = s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue()
                    except Exception:
                        continue
                    # a ducked app is quieter — scale the threshold so it still counts
                    thr = MEDIA_PEAK * (DUCK_TO if proc.pid in self._ducked else 1.0)
                    if peak > thr:
                        loud = True
                now = time.time()
                if loud:
                    self._media_seen_at = now
                self.media_playing = (now - self._media_seen_at) < MEDIA_HOLD_SECS

                active = now < self._active_until
                if active and (self._want_duck or self.media_playing) and not self._ducked:
                    self._duck(sessions, my_pid)
                elif not active and self._ducked:
                    self._restore()
                    self._want_duck = False
                self._notify()
            except Exception as e:
                print(f"[VoiceFocus] ⚠️ {e}")
            time.sleep(0.25)

    def _duck(self, sessions, my_pid):
        for s in sessions:
            proc = s.Process
            if proc is None or proc.pid == my_pid or s.SimpleAudioVolume is None:
                continue
            try:
                vol = s.SimpleAudioVolume
                orig = vol.GetMasterVolume()
                target = orig * DUCK_TO
                vol.SetMasterVolume(target, None)
                self._ducked[proc.pid] = (vol, orig, target)
            except Exception:
                continue
        if self._ducked:
            print(f"[VoiceFocus] 🔉 ducked {len(self._ducked)} app(s)")

    def _restore(self):
        for pid, (vol, orig, set_to) in list(self._ducked.items()):
            try:
                # don't stomp on a volume the user changed meanwhile
                if abs(vol.GetMasterVolume() - set_to) < 0.02:
                    vol.SetMasterVolume(orig, None)
            except Exception:
                pass
        self._ducked.clear()
        print("[VoiceFocus] 🔊 volume restored")

    def _hotkey_loop(self):
        """Global Ctrl+Alt+B — works whatever window is focused."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 0xB41C, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_B):
                print(f"[VoiceFocus] ⚠️ couldn't register {HOTKEY_LABEL} (another app owns it)")
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:   # WM_HOTKEY
                    self.activate(HOTKEY_LABEL)
        except Exception as e:
            print(f"[VoiceFocus] ⚠️ hotkey: {e}")

    def shutdown(self, wait: float = 1.0):
        """Close the talk window so the focus thread restores ducked volumes
        (COM objects belong to that thread). Call before exiting."""
        with self._lock:
            self._active_until = 0.0
        deadline = time.time() + wait
        while self._ducked and time.time() < deadline:
            time.sleep(0.05)


_instance: VoiceFocus | None = None


def get_voice_focus(ui=None) -> VoiceFocus:
    global _instance
    if _instance is None:
        _instance = VoiceFocus(ui)
    return _instance

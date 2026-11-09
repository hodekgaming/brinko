# actions/wake_word.py
#
# Runs a tiny parallel audio capture thread that watches for the wake phrase
# "Hey Brinko" using openwakeword (free, fully offline, runs on CPU).
# Completely independent of the main Gemini Live voice session — they open
# separate PortAudio streams so they never interfere with each other.
#
# When the phrase is detected:
#   1. The mic is unmuted (so the Gemini Live session receives audio again)
#   2. A short chime plays (so the user knows BRINKO woke up)
#   3. The HUD pulses to signal activation
#
# openwakeword is available at: pip install openwakeword
# On first run it downloads ~30 MB of pre-trained models to
# ~/.local/share/openwakeword — after that it's fully offline.
#
# HONEST LIMITS:
# - False-positive rate is low but not zero — a TV or music with similar
#   sounds can occasionally trigger it. If that's a problem, bump
#   THRESHOLD toward 0.6-0.7.
# - The "hey_brinko" phrase won't have a dedicated pre-trained model (you'd
#   need to record ~30 training utterances and train one — a 20-minute
#   process, instructions in the README). By default this uses "hey_jarvis"
#   which the openwakeword team ships pre-trained and which works very
#   similarly. You can swap WAKE_PHRASE_MODEL to any model from:
#   https://github.com/dscripka/openWakeWord#pre-trained-models
#
# How to train your own "hey_brinko" model (optional but 20 min setup):
#   pip install openwakeword[training]
#   python -c "from openwakeword.utils import generate_adversarial_set; ..."
#   -- see the openwakeword docs for the full flow. Put the resulting .onnx
#   file at config/hey_brinko.onnx and set WAKE_PHRASE_MODEL below.

import os
import sys
import time
import threading
import struct
import platform
from pathlib import Path

# --- tunable constants ---
WAKE_PHRASE_MODEL   = "hey_jarvis"  # closest pre-trained model; swap to your .onnx path
SAMPLE_RATE         = 16000          # openwakeword requires 16kHz
CHANNELS            = 1
CHUNK_SIZE          = 1280           # 80ms at 16kHz — the standard openwakeword frame size
THRESHOLD           = 0.45           # 0.0–1.0; lower = more sensitive, higher = fewer false triggers
COOLDOWN_SECS       = 3.0            # ignore further triggers for this many seconds after one fires


def _play_activation_chime():
    """Plays a short 'ready' tone so the user knows the wake word was heard.
    Uses stdlib only — no extra audio library needed for a simple beep."""
    try:
        if platform.system() == "Windows":
            import winsound
            winsound.Beep(880, 120)
            winsound.Beep(1100, 100)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


def start_wake_word_listener(ui, brinko_instance=None) -> threading.Thread | None:
    """
    Starts the wake-word detector in a daemon thread. Call once from
    main.py after the BrinkoLive instance exists. Returns the thread so
    the caller can check its state, or None if openwakeword or sounddevice
    aren't installed.
    """
    try:
        import openwakeword
        from openwakeword.model import Model as OWWModel
    except ImportError:
        ui.write_log(
            "SYS: Wake word disabled — install openwakeword: pip install openwakeword"
        )
        return None

    try:
        import sounddevice as sd
    except ImportError:
        ui.write_log("SYS: Wake word disabled — sounddevice not installed.")
        return None

    # Resolve model — try user's custom .onnx first (config/hey_brinko.onnx),
    # then the pre-trained model by name.
    custom = Path(__file__).resolve().parent.parent / "config" / "hey_brinko.onnx"
    if custom.exists():
        try:
            oww_model = OWWModel(wakeword_models=[str(custom)], inference_framework="onnx")
            model_label = "hey_brinko (custom)"
        except Exception as e:
            ui.write_log(f"SYS: Custom wake model failed to load ({e}), falling back to hey_jarvis")
            oww_model = None
    else:
        oww_model = None

    if oww_model is None:
        try:
            oww_model = OWWModel(wakeword_models=[WAKE_PHRASE_MODEL], inference_framework="onnx")
            model_label = WAKE_PHRASE_MODEL
        except Exception as e:
            ui.write_log(f"SYS: Wake word model load failed: {e}")
            return None

    # "Hey Brinko": offline Vosk speech recognition limited to a small grammar with look-alike
    # decoys (drink / brick / think / bring...), so only the real phrase can win.
    # Tested: triggers on "Hey Brinko(, open Spotify)" and "Hey Brink", not on "on the brink of war",
    # "hey, bring me a drink", "Hey Pinko", "I think the brick is pink" (Nov 9 2026, Windows TTS voices).
    vosk_rec = None
    try:
        import json as _json
        from vosk import Model as _VModel, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        _vdir = Path(__file__).resolve().parent.parent / "models" / "vosk-model-small-en-us-0.15"
        if _vdir.exists():
            _grammar = _json.dumps(["hey brink", "brink", "hey", "think", "drink", "bring", "brick", "pink",
                                    "blink", "jarvis", "hey jarvis", "the", "a", "[unk]"])
            _vmodel = _VModel(str(_vdir))
            vosk_rec = KaldiRecognizer(_vmodel, SAMPLE_RATE, _grammar)
            model_label += " + hey brinko"
    except Exception as e:
        print(f"[WakeWord] 'Hey Brinko' unavailable: {e}")

    ui.write_log(f"SYS: Wake word active — model: {model_label}")

    def _listener():
        _last_trigger = 0.0
        _errs = [0]
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
            ) as stream:
                ui.write_log("SYS: Wake word stream open. Say 'Hey Brinko' or 'Hey Jarvis' to activate.")
                while True:
                    chunk, _ = stream.read(CHUNK_SIZE)
                    # openwakeword needs an int16 numpy array — a Python list
                    # raises ValueError (which used to be swallowed below, so
                    # the wake word could never fire).
                    pcm = chunk.reshape(-1)

                    try:
                        predictions = oww_model.predict(pcm)
                    except Exception as e:
                        if not _errs[0]:
                            print(f"[WakeWord] ⚠️ predict failed: {e}")
                        _errs[0] += 1
                        continue

                    score = max(predictions.values()) if predictions else 0.0

                    if vosk_rec is not None:
                        try:
                            heard = ""
                            if vosk_rec.AcceptWaveform(pcm.tobytes()):
                                heard = _json.loads(vosk_rec.Result()).get("text", "")
                            else:
                                heard = _json.loads(vosk_rec.PartialResult()).get("partial", "")
                            if "hey brink" in heard:      # "Hey Brinko" comes out as "hey brink a" (not in the vocabulary)
                                vosk_rec.Reset()
                                score = max(score, 0.99)
                        except Exception as e:
                            if not _errs[0]:
                                print(f"[WakeWord] vosk: {e}")
                            _errs[0] += 1

                    if score >= THRESHOLD:
                        now = time.time()
                        if now - _last_trigger < COOLDOWN_SECS:
                            continue
                        _last_trigger = now

                        print(f"[WakeWord] 🎤 Triggered ({model_label}, score={score:.2f})")
                        ui.write_log(f"SYS: Wake word detected (score {score:.2f}) — activating BRINKO.")

                        # Unmute only if currently muted
                        if ui.muted:
                            ui.muted = False

                        # Saying the wake word while BRINKO talks cuts it off at once.
                        if brinko_instance is not None and getattr(brinko_instance, "_is_speaking", False):
                            try:
                                brinko_instance.interrupt()
                            except Exception as e:
                                print(f"[WakeWord] interrupt: {e}")

                        # Open the mic + duck other audio (voice focus). Don't
                        # speak() here — text sent that way reads as user speech.
                        if brinko_instance is not None and getattr(brinko_instance, "focus", None):
                            brinko_instance.focus.activate("wake word")

                        _play_activation_chime()

        except Exception as e:
            print(f"[WakeWord] ⚠️ Stream error: {e}")
            ui.write_log(f"SYS: Wake word listener stopped: {e}")

    t = threading.Thread(target=_listener, daemon=True, name="WakeWord")
    t.start()
    return t

# or_client.py — text + vision LLM client used by every tool (not the voice).
#
# Order of attempts:
#   1. Gemini (fast, reliable, uses the Gemini key — a separate quota from the
#      Live voice model)
#   2. OpenRouter free models — the list is fetched LIVE from OpenRouter,
#      because free slugs come and go constantly (a hard-coded list rotted
#      into 100% HTTP 404s and spammed the console).
# Models that 404 are marked dead for the session and never retried.

import json
import sys
import time
import base64
import logging
import threading
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("openrouter_client")
logger.setLevel(logging.WARNING)
# per-request INFO lines from these libraries flooded the console
for _noisy in ("httpx", "google_genai", "google_genai.models", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR     = _get_base_dir()
API_KEY_PATH = BASE_DIR / "config" / "api_keys.json"


def _load_keys() -> dict:
    try:
        with open(API_KEY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"api_keys.json not found at: {API_KEY_PATH}")
    except Exception as e:
        raise RuntimeError(f"Failed to read api_keys.json: {e}")


def _load_api_key() -> str:
    key = str(_load_keys().get("openrouter_api_key", "")).strip()
    if not key:
        raise RuntimeError("openrouter_api_key is empty in api_keys.json")
    return key


# Gemini text/vision models, best first. Retired names (gemini-2.5-*) 404 for
# new keys; "-latest" aliases often 503 under load, so they go last.
GEMINI_MODELS: list[str] = [
    "gemini-3.1-flash-lite",     # ~0.6s, reliable on the free tier
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",          # stronger but often 503/429 on free keys
]

# Preferred OpenRouter free models — used to ORDER the live list; anything
# not live is skipped automatically.
_PREFERRED_FREE = [
    "nvidia/nemotron-3-ultra",
    "google/gemma-4-31b",
    "google/gemma-4-26b",
    "nvidia/nemotron-3.5-super",
    "xingchen-agi",
    "dots-studio",
    "nvidia/nemotron-3.5-lightning",
    "cohere/north-mini-code",
]
_SKIP_FREE = ("content-safety", "embed", "guard", "tts")

# Kept for anything that still imports these names.
TEXT_MODELS:   list[str] = []
VISION_MODELS: list[str] = []

API_URL               = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL            = "https://openrouter.ai/api/v1/models"
DEFAULT_MAX_TOKENS    = 4096
DEFAULT_TEMPERATURE   = 0.7
REQUEST_TIMEOUT       = 60
RATE_LIMIT_COOLDOWN   = 60
MODEL_LIST_TTL        = 3600
MAX_OPENROUTER_TRIES  = 3     # don't walk the whole pool — fail fast
GEMINI_TIMEOUT_MS     = 30000
# Writing/reasoning that should be GOOD rather than instant (emails, texts,
# deep thinking) tries the stronger model first and waits longer before
# racing the free OpenRouter models.
SMART_MODELS          = ["gemini-3.5-flash"]
SMART_HEDGE_SECS      = 14.0
HEDGE_AFTER_SECS      = 4.0   # Gemini slower than this → race OpenRouter in parallel
OVERALL_TIMEOUT_SECS  = 90

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")

_rate_limited: dict[str, float] = {}
_dead: set[str] = set()


class OpenRouterClient:

    def __init__(self) -> None:
        # Keys load lazily: this module is imported at startup, before the
        # first-run setup screen has had a chance to write api_keys.json.
        self._api_key: str | None = None
        self._gemini = None
        self._gemini_lock = threading.Lock()
        self._free_models: list[str] = []
        self._vision_models: list[str] = []
        self._models_at = 0.0
        self._models_lock = threading.Lock()

    # ── keys / clients ──────────────────────────────────────────────────
    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = _load_api_key()
        return self._api_key

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/brinko",
            "X-Title":       "Brinko",
        }

    def _gemini_client(self):
        with self._gemini_lock:
            if self._gemini is None:
                key = str(_load_keys().get("gemini_api_key", "")).strip()
                if not key:
                    return None
                from google import genai
                from google.genai import types
                # Hard cap per request and no hidden SDK retries — a stuck
                # request must fail fast so the fallback can answer instead.
                self._gemini = genai.Client(api_key=key, http_options=types.HttpOptions(
                    timeout=GEMINI_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ))
            return self._gemini

    # ── model bookkeeping ───────────────────────────────────────────────
    def _is_rate_limited(self, model: str) -> bool:
        ts = _rate_limited.get(model)
        if ts is None:
            return False
        if time.time() - ts > RATE_LIMIT_COOLDOWN:
            del _rate_limited[model]
            return False
        return True

    def _mark_rate_limited(self, model: str) -> None:
        _rate_limited[model] = time.time()
        logger.info(f"[LLM] rate limited: {model} — cooling down {RATE_LIMIT_COOLDOWN}s")

    def _live_free_models(self) -> tuple[list[str], list[str]]:
        with self._models_lock:
            if self._free_models and time.time() - self._models_at < MODEL_LIST_TTL:
                return self._free_models, self._vision_models
            try:
                data = requests.get(MODELS_URL, timeout=15).json().get("data", [])
            except Exception as e:
                logger.info(f"[LLM] couldn't fetch OpenRouter model list: {e}")
                return self._free_models, self._vision_models

            def rank(mid: str) -> int:
                for i, pref in enumerate(_PREFERRED_FREE):
                    if mid.startswith(pref):
                        return i
                return len(_PREFERRED_FREE)

            text, vision = [], []
            for m in data:
                mid = m.get("id", "")
                if not mid.endswith(":free") or any(s in mid for s in _SKIP_FREE):
                    continue
                text.append(mid)
                inputs = (m.get("architecture") or {}).get("input_modalities") or []
                if "image" in inputs:
                    vision.append(mid)
            text.sort(key=rank)
            vision.sort(key=rank)
            self._free_models, self._vision_models = text, vision
            TEXT_MODELS[:] = text
            VISION_MODELS[:] = vision
            self._models_at = time.time()
            return text, vision

    # ── Gemini ──────────────────────────────────────────────────────────
    def _gemini_call(self, messages: list[dict], max_tokens: int, temperature: float,
                     json_mode: bool = False, smart: bool = False) -> Optional[str]:
        try:
            gc = self._gemini_client()
        except Exception as e:
            logger.info(f"[LLM] Gemini unavailable: {e}")
            return None
        if gc is None:
            return None
        from google.genai import types

        system_parts, contents = [], []
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "system":
                system_parts.append(content if isinstance(content, str) else str(content))
                continue
            parts = []
            if isinstance(content, str):
                parts.append(types.Part.from_text(text=content))
            else:
                for c in content or []:
                    if c.get("type") == "text":
                        parts.append(types.Part.from_text(text=c.get("text", "")))
                    elif c.get("type") == "image_url":
                        url = (c.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and ";base64," in url:
                            mime, b64 = url[5:].split(";base64,", 1)
                            parts.append(types.Part.from_bytes(data=base64.b64decode(b64), mime_type=mime))
            contents.append(types.Content(role="model" if role == "assistant" else "user", parts=parts))

        cfg = types.GenerateContentConfig(
            system_instruction="\n".join(system_parts) or None,
            max_output_tokens=max_tokens,
            temperature=temperature,
            response_mime_type="application/json" if json_mode else None,
        )
        order = (SMART_MODELS + [m for m in GEMINI_MODELS if m not in SMART_MODELS]) if smart else GEMINI_MODELS
        for model in order:
            if model in _dead or self._is_rate_limited(model):
                continue
            try:
                r = gc.models.generate_content(model=model, contents=contents, config=cfg)
                text = (r.text or "").strip()
                if text:
                    return text
            except Exception as e:
                msg = str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    _dead.add(model)
                elif any(c in msg for c in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
                                            "504", "DEADLINE", "timed out", "Timeout")):
                    self._mark_rate_limited(model)
                logger.info(f"[LLM] Gemini {model} failed: {msg[:120]}")
        return None

    # ── OpenRouter ──────────────────────────────────────────────────────
    def _call(self, model: str, messages: list[dict], max_tokens: int = DEFAULT_MAX_TOKENS,
              temperature: float = DEFAULT_TEMPERATURE,
              response_format: Optional[dict] = None) -> Optional[str]:
        payload: dict = {"model": model, "messages": messages,
                         "max_tokens": max_tokens, "temperature": temperature}
        if response_format:
            payload["response_format"] = response_format
        try:
            resp = requests.post(API_URL, headers=self._headers, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            logger.info(f"[LLM] {model} timed out")
            return None
        except Exception as e:
            logger.info(f"[LLM] {model} error: {e}")
            return None

        if resp.status_code == 200:
            try:
                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception:
                return None
            return content.strip() if content else None
        if resp.status_code == 429:
            self._mark_rate_limited(model)
        elif resp.status_code in (400, 402, 403, 404):
            _dead.add(model)   # gone / not free anymore — never retry this session
        logger.info(f"[LLM] {model} → HTTP {resp.status_code}")
        return None

    def _call_with_fallback(self, pool: Optional[list[str]], messages: list[dict],
                            model: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS,
                            temperature: float = DEFAULT_TEMPERATURE,
                            response_format: Optional[dict] = None, vision: bool = False,
                            smart: bool = False) -> str:
        # An explicit OpenRouter slug ("vendor/model") gets one shot first.
        if model and "/" in model and model not in _dead and not self._is_rate_limited(model):
            result = self._call(model, messages, max_tokens, temperature, response_format)
            if result:
                return result

        def _gemini_path():
            return self._gemini_call(messages, max_tokens, temperature,
                                     json_mode=bool(response_format), smart=smart)

        def _openrouter_path():
            try:
                text_pool, vision_pool = self._live_free_models()
            except Exception:
                return None
            tries = 0
            for m in (vision_pool if vision else text_pool):
                if m in _dead or self._is_rate_limited(m):
                    continue
                r = self._call(m, messages, max_tokens, temperature, response_format)
                if r:
                    return r
                tries += 1
                if tries >= MAX_OPENROUTER_TRIES:
                    break
            return None

        # Hedged request: Gemini first; if it's slow (or fails), race
        # OpenRouter alongside it and take whichever answers first.
        pending = {_POOL.submit(_gemini_path)}
        done, pending = wait(pending, timeout=SMART_HEDGE_SECS if smart else HEDGE_AFTER_SECS)
        for f in done:
            if f.result():
                return f.result()
        pending.add(_POOL.submit(_openrouter_path))
        deadline = time.time() + OVERALL_TIMEOUT_SECS
        while pending and time.time() < deadline:
            done, pending = wait(pending, timeout=max(0.1, deadline - time.time()),
                                 return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    r = f.result()
                except Exception:
                    r = None
                if r:
                    return r

        raise RuntimeError("[LLM] All models failed or are rate-limited right now — try again in a minute.")

    # ── public API (unchanged signatures) ───────────────────────────────
    def chat(self, prompt: str,
             system: str = ("You are a helper component inside the BRINKO assistant. "
                            "Be concise, helpful, and precise."),
             model: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS,
             temperature: float = DEFAULT_TEMPERATURE, smart: bool = False) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        return self._call_with_fallback(None, messages, model, max_tokens, temperature, smart=smart)

    def chat_json(self, prompt: str,
                  system: str = "Return ONLY valid JSON. No markdown fences, no extra text, no explanation.",
                  model: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        raw = self._call_with_fallback(None, messages, model, max_tokens, temperature=0.2,
                                       response_format={"type": "json_object"})
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("`").strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            logger.error(f"[LLM] JSON parse failed: {e} — raw: {raw[:300]}")
            raise ValueError(f"Model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")

    def vision(self, prompt: str, image_b64: str, mime: str = "image/png",
               system: str = "Analyze the image and describe what you see clearly and concisely.",
               model: Optional[str] = None, max_tokens: int = 1024) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ]
        return self._call_with_fallback(None, messages, model, max_tokens, temperature=0.2, vision=True)

    def vision_from_file(self, prompt: str, image_path: str,
                         system: str = "Analyze the image and describe what you see clearly and concisely.",
                         model: Optional[str] = None, max_tokens: int = 1024) -> str:
        path = Path(image_path)
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/png")
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(self, messages: list[dict], model: Optional[str] = None,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   temperature: float = DEFAULT_TEMPERATURE) -> str:
        return self._call_with_fallback(None, messages, model, max_tokens, temperature)

    def available_models(self) -> dict:
        text, vision = self._free_models, self._vision_models
        return {
            "gemini_models": GEMINI_MODELS,
            "text_models":   text,
            "vision_models": vision,
            "dead":          sorted(_dead),
            "rate_limited":  list(_rate_limited.keys()),
            "total_text":    len(text),
            "total_vision":  len(vision),
        }


client = OpenRouterClient()

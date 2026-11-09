# actions/ad_content.py
# Advertising / social-media content generation — real generated images,
# render prompts for external tools, and competitive ad-fatigue analysis.

import sys
import json
from pathlib import Path
from datetime import datetime


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DESKTOP     = Path.home() / "Desktop"
CONFIG_PATH = _get_base_dir() / "config" / "api_keys.json"

# Tried in order. Google renames/versions these periodically, so rather than
# betting everything on one guessed identifier, we try several known-recent
# candidates for each approach and use whichever the account/region/SDK
# version actually supports.
IMAGEN_MODEL_CANDIDATES = [
    "imagen-4.0-generate-001",
    "imagen-3.0-generate-002",
]
FLASH_IMAGE_MODEL_CANDIDATES = [
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
    "gemini-2.0-flash-preview-image-generation",
    "gemini-2.0-flash-exp",
]


def _get_gemini_key() -> str:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _generate_image_bytes(prompt: str, log=None) -> bytes:
    """
    Actually generates image pixels (not just a text prompt for you to run
    elsewhere). Tries several Imagen model versions first (purpose-built
    for image quality), then several Gemini multimodal image-generation
    model versions — model naming/availability shifts by account, region,
    and SDK version, so covering a few candidates beats hardcoding one
    guess and failing silently when it's wrong.
    """
    from google import genai
    from google.genai import types

    def _log(msg: str):
        print(f"[AdContent] {msg}")
        if log:
            try:
                log(msg)
            except Exception:
                pass

    gclient = genai.Client(api_key=_get_gemini_key())
    attempts: list[str] = []

    for model_name in IMAGEN_MODEL_CANDIDATES:
        try:
            _log(f"Trying Imagen model: {model_name}...")
            response = gclient.models.generate_images(
                model=model_name,
                prompt=prompt,
                config=types.GenerateImagesConfig(number_of_images=1),
            )
            data = getattr(response.generated_images[0].image, "image_bytes", None)
            if data:
                _log(f"✅ Image generated via {model_name}")
                return data
            attempts.append(f"{model_name}: returned no image bytes")
        except Exception as e:
            attempts.append(f"{model_name}: {e}")
            _log(f"⚠️ {model_name} failed: {str(e)[:150]}")

    for model_name in FLASH_IMAGE_MODEL_CANDIDATES:
        try:
            _log(f"Trying Gemini image model: {model_name}...")
            response = gclient.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
            )
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                attempts.append(f"{model_name}: no candidates in response")
                continue
            parts = getattr(candidates[0].content, "parts", None) or []
            for part in parts:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    _log(f"✅ Image generated via {model_name}")
                    return inline.data
            attempts.append(f"{model_name}: response had no image data")
        except Exception as e:
            attempts.append(f"{model_name}: {e}")
            _log(f"⚠️ {model_name} failed: {str(e)[:150]}")

    detail = "; ".join(attempts) if attempts else "no models attempted"
    raise RuntimeError(f"All image models unavailable — {detail}")


def _make_image(description: str, style: str, log=None) -> Path:
    prompt = description.strip()
    if style:
        prompt += f"\n\nStyle: {style}"

    image_bytes = _generate_image_bytes(prompt, log=log)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DESKTOP / f"pixel_image_{ts}.png"
    DESKTOP.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)
    return path


def _concept(brief: str, platform_: str, count: int) -> str:
    from or_client import client
    prompt = (
        f"You are a senior social media ad creative director.\n"
        f"Platform: {platform_}\n"
        f"Brief: {brief}\n\n"
        f"Generate {count} distinct ad creative concepts. For each concept give:\n"
        f"1. A short concept name\n"
        f"2. The visual idea (1-2 sentences)\n"
        f"3. A ready-to-use AI image-generation render prompt (detailed, "
        f"style + composition + lighting)\n"
        f"4. A one-line hook/caption\n\n"
        f"Format clearly with numbered sections."
    )
    return client.chat(
        prompt,
        system="You are an expert advertising creative director and prompt engineer.",
        max_tokens=2048,
    )


def _render_prompt(description: str, style: str) -> str:
    from or_client import client
    prompt = (
        f"Write a single, highly detailed AI image-generation render prompt "
        f"for this ad visual:\n\n{description}\n\n"
        f"Style: {style or 'photorealistic, high-end commercial photography'}\n"
        f"Include composition, lighting, camera/lens detail, color palette, and mood. "
        f"Return ONLY the prompt text, nothing else."
    )
    return client.chat(prompt, system="You write precise AI image-gen prompts.", max_tokens=400)


def _ad_fatigue(brand_or_niche: str) -> str:
    from actions.web_search import web_search as _search
    raw = _search(parameters={
        "query": f"{brand_or_niche} recent ad campaigns creative examples 2026"
    })
    from or_client import client
    prompt = (
        f"Based on this research about '{brand_or_niche}' advertising:\n\n{raw[:6000]}\n\n"
        f"Analyze competitive ad fatigue: which creative angles/formats are "
        f"overused right now, and suggest 3 fresh angles that would stand out. "
        f"Be concise and specific."
    )
    return client.chat(prompt, system="You are an ad strategy analyst.", max_tokens=900)


def _save(content: str, prefix: str) -> Path:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DESKTOP / f"{prefix}_{ts}.txt"
    DESKTOP.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def ad_content(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action      : image | concept | render_prompt | ad_fatigue (default: concept)
        description : what to actually generate an image of (for action=image), or
                      what to write a render prompt for (for action=render_prompt)
        style       : visual style hint (for image / render_prompt)
        brief       : product/campaign brief (for concept)
        platform    : Instagram | TikTok | Facebook | X (optional)
        count       : number of concepts to generate (default 3)
        brand       : brand or niche name (for ad_fatigue)
        save        : bool, save text output to Desktop (default False; images always save)
    """
    p      = parameters or {}
    action = p.get("action", "concept").lower().strip()
    save   = bool(p.get("save", False))

    if player:
        player.write_log(f"[AdContent] {action}")

    try:
        if action == "image":
            desc = p.get("description", "").strip()
            if not desc:
                return "Please describe the image you want made, sir."
            log_fn = player.write_log if player else None
            path = _make_image(desc, p.get("style", ""), log=log_fn)
            return f"Image generated and saved to: {path}"

        if action == "concept":
            brief = p.get("brief", "").strip()
            if not brief:
                return "Please describe the product or campaign brief, sir."
            result = _concept(
                brief,
                p.get("platform", "Instagram"),
                int(p.get("count", 3)),
            )

        elif action == "render_prompt":
            desc = p.get("description", "").strip()
            if not desc:
                return "Please describe the visual you want a render prompt for, sir."
            result = _render_prompt(desc, p.get("style", ""))

        elif action == "ad_fatigue":
            brand = p.get("brand", "").strip()
            if not brand:
                return "Please specify a brand or niche to analyze, sir."
            result = _ad_fatigue(brand)

        else:
            return f"Unknown ad_content action: '{action}'."

        if save:
            path = _save(result, action)
            result += f"\n\nSaved to: {path}"

        return result

    except Exception as e:
        return f"Ad content generation failed, sir: {e}"

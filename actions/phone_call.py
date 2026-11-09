# actions/phone_call.py
#
# Makes real phone calls that can hold a full back-and-forth conversation,
# handle "What name for the reservation?", "We're fully booked at 7, how
# about 7:30?" etc. — the kind of thing that requires an actual AI voice
# agent on the line, not just dialing.
#
# Uses Bland.ai (https://bland.ai) — purpose-built for exactly this.
# A free account gets 1 hour of call time to start. After that it's
# pay-as-you-go (~$0.12/min for US/CA). You need a free account and an
# API key from app.bland.ai/dashboard/api-keys.
#
# Alternative: VAPI (vapi.ai) works almost identically and can swap in
# by changing _call_bland to hit their endpoint instead.
#
# HONEST LIMITS:
# - Restaurant must have a real phone number (not online-only booking).
# - The AI is very good but can struggle with heavy background noise,
#   unusual hold music, or systems that demand very specific button presses.
# - International calls work (different per-minute rate) but need the
#   country code in the number: +33-1-... for France etc.
# - Call recordings/transcripts are accessible at app.bland.ai after the
#   call — Bland stores them by default. If privacy matters, disable in
#   your Bland dashboard under "Recording Settings."

import json
import sys
from pathlib import Path

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _get_base_dir() / "config" / "api_keys.json"

_SETUP_MESSAGE = (
    "To make AI phone calls, sir, I need a Bland.ai API key. "
    "Create a free account at bland.ai, get your key from "
    "app.bland.ai/dashboard/api-keys, then add it to config/api_keys.json as:\n"
    '  "bland_api_key": "your-key-here"\n'
    "Restart me after saving. Bland gives you 1 hour of free calls to start."
)

_DEFAULT_VOICE = "nat"       # Bland's recommended natural-sounding default
_DEFAULT_MAX_DURATION = 12   # minutes; 12 is plenty for a reservation


def _get_bland_key() -> str | None:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("bland_api_key") or None
    except Exception:
        return None


def _normalise_phone(number: str) -> str:
    """Adds a leading + if absent, strips spaces/dashes/parens."""
    import re
    number = re.sub(r"[\s\-\(\).]", "", number)
    if number and number[0] != "+":
        number = "+" + number
    return number


def _call_bland(
    phone: str,
    task: str,
    voice: str = _DEFAULT_VOICE,
    max_duration: int = _DEFAULT_MAX_DURATION,
    api_key: str | None = None,
) -> dict:
    """
    Initiates the AI call. Returns Bland's response dict. The call
    completes asynchronously — we get call_id back immediately and can
    poll for status or wait for a webhook.
    """
    response = requests.post(
        "https://api.bland.ai/v1/calls",
        headers={
            "authorization": api_key,
            "Content-Type":  "application/json",
        },
        json={
            "phone_number":    phone,
            "task":            task,
            "voice":           voice,
            "max_duration":    max_duration,
            "answered_by_enabled": True,  # detects voicemail vs human
            "wait_for_greeting": True,    # waits for "Hello?" before speaking
            "record": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _poll_status(call_id: str, api_key: str) -> dict:
    """Returns the current call metadata from Bland."""
    response = requests.get(
        f"https://api.bland.ai/v1/calls/{call_id}",
        headers={"authorization": api_key},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _build_reservation_task(
    restaurant_name: str,
    party_size: int,
    date: str,
    time: str,
    name: str,
    notes: str,
) -> str:
    return (
        f"You are calling {restaurant_name} to make a dinner reservation. "
        f"Here are the details: "
        f"Party size: {party_size} people. "
        f"Date: {date}. "
        f"Time: {time}. "
        f"Name for the reservation: {name}. "
        + (f"Special requests: {notes}. " if notes else "") +
        "If your first-choice time is unavailable, accept an alternative within "
        "30 minutes earlier or later if offered. "
        "Confirm the reservation details before ending the call. "
        "If asked for a phone number to confirm, politely decline to leave one "
        "and say you'll call back to confirm. "
        "Be polite and concise."
    )


def phone_call(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action          : reserve | call | status  (default: call for general, reserve for restaurants)
        phone           : phone number (with or without country code)
        task            : what to say/accomplish (for action=call — freeform)
        restaurant_name : restaurant name (for action=reserve)
        party_size      : number of people (for reserve, default 2)
        date            : date for the reservation, e.g. "this Friday" or "August 15"
        time            : desired time, e.g. "7pm" or "19:00"
        name            : name for the reservation (defaults to "the caller")
        notes           : special requests, e.g. "window table", "birthday"
        call_id         : for action=status — poll a previous call
    """
    if not _REQUESTS_OK:
        return "I need the requests library, sir: pip install requests"

    p       = parameters or {}
    action  = p.get("action", "").lower().strip()
    api_key = _get_bland_key()

    if not api_key:
        return _SETUP_MESSAGE

    if player:
        player.write_log(f"[PhoneCall] {action or 'call'}")

    try:
        if action == "status":
            call_id = p.get("call_id", "").strip()
            if not call_id:
                return "I need a call_id to check status, sir."
            data = _poll_status(call_id, api_key)
            status    = data.get("status", "unknown")
            duration  = data.get("call_length")
            answered  = data.get("answered_by", "unknown")
            transcript = data.get("transcripts", [])
            lines = [f"Call {call_id[:8]}… — status: {status}"]
            if duration:
                lines.append(f"Duration: {duration:.0f}s")
            if answered:
                lines.append(f"Answered by: {answered}")
            if transcript:
                lines.append(f"Last exchange: {transcript[-1].get('text', '')[:120]}")
            return "\n".join(lines)

        phone = _normalise_phone(p.get("phone", "").strip())
        if not phone or phone == "+":
            return "I need a phone number to call, sir."

        if action == "reserve":
            restaurant = p.get("restaurant_name", "the restaurant").strip()
            party_size = int(p.get("party_size", 2))
            date       = p.get("date", "tonight").strip()
            time_str   = p.get("time", "7pm").strip()
            name       = p.get("name", "").strip() or "the caller"
            notes      = p.get("notes", "").strip()

            task = _build_reservation_task(restaurant, party_size, date, time_str, name, notes)

            if speak:
                speak(f"Calling {restaurant} to book a table for {party_size}, sir.")

        else:
            task = p.get("task", "").strip()
            if not task:
                return "What should I say on the call, sir?"
            if speak:
                speak(f"Placing the call now, sir.")

        data    = _call_bland(phone, task, api_key=api_key)
        call_id = data.get("call_id", "")
        status  = data.get("status", "")

        if not call_id:
            err = data.get("message") or data.get("error") or json.dumps(data)[:120]
            return f"Call failed to start, sir: {err}"

        return (
            f"Call placed, sir — ID: {call_id[:12]}…\n"
            f"Status: {status}\n"
            f"I'll handle the conversation. Say 'call status {call_id[:8]}' "
            f"to check how it's going, or check your Bland dashboard for a full transcript."
        )

    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json().get("message", "")
        except Exception:
            pass
        return f"Phone call failed, sir: {e} {body}".strip()
    except Exception as e:
        return f"Phone call failed, sir: {e}"

# agent/personas.py
#
# HONESTY NOTE: there is one voice model (Gemini Live) doing the actual
# thinking and one set of tools underneath. True separate models per agent
# would mean separate Live sessions, separate costs, and a much more
# complex handoff system. What this module gives you instead is a real
# UI + speech layer: every tool is labeled with a named specialist, BRINKO
# announces the handoff, the UI lights up that agent's node in the ring,
# and roll_call makes all ten answer. It genuinely changes what you see and
# hear — it just isn't ten independently-reasoning AIs.

# The 10 nodes drawn around BRINKO in the UI, in ring order.
AGENT_RING: list[str] = [
    "DEXTER",   # code & dev
    "ATLAS",    # files & desktop
    "FRIDAY",   # research
    "VERA",     # vision / screen analysis
    "ZOEY",     # messaging & browsing
    "NOVA",     # streaming setup
    "ECHO",     # system control
    "TITAN",    # games
    "PIXEL",    # ad / creative content
    "SAGE",     # memory, reminders, meta
]

AGENT_ROLE: dict[str, str] = {
    "DEXTER": "code and development",
    "ATLAS":  "files and desktop management",
    "FRIDAY": "research and information",
    "VERA":   "screen and vision analysis",
    "ZOEY":   "messaging and browsing",
    "NOVA":   "streaming setup",
    "ECHO":   "system control",
    "TITAN":  "games",
    "PIXEL":  "ad, creative content, and real image generation",
    "SAGE":   "memory and coordination",
}

# One distinct hue per agent, used by ui.py to render each node in the
# mind-map ring as its own color instead of a single uniform accent.
AGENT_COLORS: dict[str, str] = {
    "DEXTER": "#ff6b00",   # orange
    "ATLAS":  "#ffd60a",   # gold
    "FRIDAY": "#00d4ff",   # cyan
    "VERA":   "#b388ff",   # violet
    "ZOEY":   "#ff4fa3",   # pink
    "NOVA":   "#7c4dff",   # indigo
    "ECHO":   "#00ff88",   # green
    "TITAN":  "#ff3355",   # red
    "PIXEL":  "#00e5c8",   # teal
    "SAGE":   "#c6ff00",   # lime
}

# A distinct node shape per agent, so each is visually different even
# without color — "circle", "square", "rounded_square", "diamond",
# "triangle", "pentagon", "hexagon", "octagon", "star", "ring" are the
# supported values (see ui.py's _draw_agent_shape).
AGENT_SHAPES: dict[str, str] = {
    "DEXTER": "hexagon",        # engineering / precision
    "ATLAS":  "square",         # structural / storage
    "FRIDAY": "circle",         # classic all-seeing research
    "VERA":   "diamond",        # sharp, eye-like — vision
    "ZOEY":   "pentagon",       # dynamic, social
    "NOVA":   "star",           # broadcast / streaming
    "ECHO":   "octagon",        # control / stop-precision
    "TITAN":  "triangle",       # bold, aggressive — games
    "PIXEL":  "rounded_square", # pixel-grid feel — image gen
    "SAGE":   "ring",           # concentric — memory / coordination
}

AGENT_PERSONAS: dict[str, str] = {
    # DEXTER — build & fix things
    "code_helper":        "DEXTER",
    "dev_agent":           "DEXTER",

    # ATLAS — files, folders, desktop, local commands
    "file_controller":     "ATLAS",
    "file_processor":      "ATLAS",
    "create_document":     "PIXEL",
    "screensaver":         "NOVA",
    "camera_powers":       "VERA",
    "open_file":           "ATLAS",
    "desktop_control":     "ATLAS",
    "cmd_control":         "ATLAS",

    # FRIDAY — research & information
    "web_search":          "FRIDAY",
    "deep_think":          "FRIDAY",
    "youtube_video":        "FRIDAY",
    "flight_finder":        "FRIDAY",

    # VERA — vision
    "screen_process":       "VERA",

    # ZOEY — people & the outside web
    "send_message":         "ZOEY",
    "email_agent":          "ZOEY",
    "phone_call":           "ZOEY",
    "music":                "ECHO",
    "browser_control":      "ZOEY",

    # NOVA — streaming
    "stream_control":       "NOVA",

    # ECHO — the machine itself
    "computer_settings":    "ECHO",
    "computer_control":     "ECHO",
    "open_app":             "ECHO",

    # TITAN — games
    "game_updater":         "TITAN",

    # PIXEL — creative / ad content
    "ad_content":           "PIXEL",

    # SAGE — memory, reminders, weather briefing, meta-control
    "weather_report":       "SAGE",
    "reminder":             "SAGE",
    "schedule_task":        "SAGE",
    "night_mode":           "SAGE",
    "save_memory":          "SAGE",
    "ultra_mode":           "SAGE",
    "agent_task":           "SAGE",

    # Everything else (roll_call itself, shutdown, etc.) stays with BRINKO.
}

DEFAULT_PERSONA = "BRINKO"


def persona_for(tool_name: str) -> str:
    return AGENT_PERSONAS.get(tool_name, DEFAULT_PERSONA)


def persona_prefix(tool_name: str) -> str:
    """e.g. '[DEXTER]' — for log lines and printouts."""
    return f"[{persona_for(tool_name)}]"


def persona_handoff_line(tool_name: str) -> str:
    """
    A short spoken handoff line BRINKO can say before delegating to a
    specialist, e.g. 'Dexter's on it, sir.' The system prompt tells the
    model to use one of these (or something similar) instead of generic
    filler before slow tools.
    """
    persona = persona_for(tool_name)
    if persona == DEFAULT_PERSONA:
        return ""
    return f"{persona.title()}'s on it, sir."


def roll_call_lines() -> list[str]:
    return [f"{name}, ready to work, all systems up." for name in AGENT_RING]


def roll_call_text() -> str:
    return " ".join(roll_call_lines())

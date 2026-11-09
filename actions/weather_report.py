# actions/weather_report.py

from urllib.parse import quote_plus

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

_WMO_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with rime", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms",
}


def get_weather_summary(city: str) -> dict | None:
    """
    Fetches real current weather for a city, no API key required
    (Open-Meteo geocoding + forecast). Returns None on failure so callers
    can fall back gracefully instead of crashing a greeting/voice reply.
    """
    if not _REQUESTS_OK or not city:
        return None
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1}, timeout=6
        ).json()
        results = geo.get("results") or []
        if not results:
            return None
        lat, lon   = results[0]["latitude"], results[0]["longitude"]
        found_name = results[0].get("name", city)

        fc = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
            timeout=6,
        ).json()

        cur   = fc.get("current", {})
        daily = fc.get("daily", {})
        code  = int(cur.get("weather_code", -1))

        return {
            "city":       found_name,
            "temp_c":     cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "wind_kmh":   cur.get("wind_speed_10m"),
            "condition":  _WMO_CODES.get(code, "unclear skies"),
            "high_c":     (daily.get("temperature_2m_max") or [None])[0],
            "low_c":      (daily.get("temperature_2m_min") or [None])[0],
        }
    except Exception as e:
        print(f"[Weather] ⚠️ Fetch failed: {e}")
        return None


def format_weather_spoken(w: dict) -> str:
    if not w or w.get("temp_c") is None:
        return ""
    return (
        f"It's currently {w['temp_c']:.0f}°C and {w['condition']} in {w['city']}, "
        f"feels like {w['feels_like']:.0f}°C, with a high of {w['high_c']:.0f}°C "
        f"and a low of {w['low_c']:.0f}°C today."
    )


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None
):
    """
    Weather report action — fetches and SPEAKS real current weather data.
    Falls back to opening a browser search only if the data fetch fails.
    """

    city = parameters.get("city")
    time = parameters.get("time")
    if not city or not isinstance(city, str):
        msg = "Sir, the city is missing for the weather report."
        _speak_and_log(msg, player)
        return msg

    city = city.strip()
    time = time.strip() if isinstance(time, str) and time.strip() else "today"

    data = get_weather_summary(city)
    if data:
        msg = format_weather_spoken(data)
        _speak_and_log(msg, player)
        if session_memory:
            try:
                session_memory.set_last_search(query=f"weather in {city}", response=msg)
            except Exception:
                pass
        return msg

    # Fallback: couldn't fetch real data — open a browser search instead.
    search_query  = f"weather in {city} {time}"
    encoded_query = quote_plus(search_query)
    url = f"https://www.google.com/search?q={encoded_query}"
    try:
        from actions.browser_util import open_url
        open_url(url)
        msg = f"Couldn't fetch live data, sir — showing the weather for {city} in the browser instead."
    except Exception:
        msg = "Sir, I couldn't retrieve the weather or open the browser."
    _speak_and_log(msg, player)
    return msg


def _speak_and_log(message: str, player=None):
    if player:
        try:
            player.write_log(f"BRINKO: {message}")
        except Exception:
            pass

#web_search.py
import json
import sys
from pathlib import Path

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _gemini_search(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 5) -> list[dict]:
    """News results carry dates — what 'latest/today' questions need."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    out = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                out.append({"title": r.get("title", ""), "snippet": r.get("body", ""),
                            "url": r.get("url", ""), "date": (r.get("date") or "")[:10]})
    except Exception as e:
        print(f"[WebSearch] news search failed: {e}")
    return out


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()

def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
    return "\n".join(lines)

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query, sir."

    if items and mode != "compare":
        mode = "compare"

    if player:
        player.write_log(f"[Search] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 Query: {query!r}  Mode: {mode}")

    if mode == "compare" and items:
        return _compare(items, aspect)

    # 1) Actually search the web. (This used to ask an LLM from memory and
    #    call it "search" → confidently outdated answers. Google Search
    #    grounding has no quota on the free Gemini tier, so DuckDuckGo it is.)
    try:
        results = _ddg_search(query, max_results=7)
        recent = any(w in query.lower() for w in ("latest", "recent", "today", "now", "current", "news",
                                                  "this week", "yesterday", "score", "won", "aktuell", "heute"))
        if recent:
            # news search is noisy (unrelated headlines) — keep only items that
            # actually mention the question's key words
            stop = {"who", "what", "when", "where", "the", "most", "recent", "latest", "today", "now", "current",
                    "news", "won", "win", "did", "does", "is", "was", "a", "an", "of", "in", "on", "for", "and"}
            keys = {w for w in query.lower().replace("?", " ").split() if len(w) > 2 and w not in stop}
            news = [n for n in _ddg_news(query, max_results=8)
                    if sum(k in (n.get("title", "") + " " + n.get("snippet", "")).lower() for k in keys) >= max(1, min(2, len(keys)))]
            results = news + results
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG failed: {e}")
        results = []
    if not results:
        return f"I couldn't reach any search results for '{query}' right now."

    # 2) Answer ONLY from those results, knowing today's date.
    from datetime import datetime
    snippets = "\n".join(
        f"[{i + 1}] {r.get('title', '')} ({r.get('date', '') or r.get('url', '')})\n{r.get('snippet', '')}"
        for i, r in enumerate(results[:11]))
    try:
        from or_client import client
        answer = client.chat(
            f"Today is {datetime.now():%A %d %B %Y}.\nQuestion: {query}\n\nFresh web search results:\n{snippets}\n\n"
            "Answer the question using ONLY these results (prefer the newest). Read carefully — e.g. a "
            "headline saying someone 'became champion at' a race does NOT mean they won that race. Be concrete: "
            "names, numbers, dates. If results disagree or don't answer it, say so. 2–4 sentences, no links.",
            system="You are a precise research assistant who never answers from memory when results are given.",
            max_tokens=1500, temperature=0.0,   # fast model + careful-reading instruction (smart model took 15-30s)
        )
        print(f"[WebSearch] ✅ answered from {len(results)} live result(s).")
        return answer
    except Exception as e:
        print(f"[WebSearch] ⚠️ summarising failed ({e}) — returning raw results.")
        return _format_ddg(query, results)

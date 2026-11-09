# actions/deep_think.py
#
# The Live voice model is fast but shallow. For anything that needs real
# reasoning — maths, planning, comparisons, advice, debugging, "explain how
# X works", decisions — BRINKO hands the question to a stronger model that
# thinks before answering, then speaks a short version of the result.

from __future__ import annotations

from datetime import datetime


def deep_think(parameters=None, player=None, **_) -> str:
    p = parameters or {}
    question = str(p.get("question") or "").strip()
    context  = str(p.get("context") or "").strip()
    if not question:
        return "What should I think through?"
    if player:
        try:
            player.write_log(f"[think] {question[:80]}")
        except Exception:
            pass

    profile = ""
    try:
        from actions.smart_writer import _user_profile
        profile = _user_profile()
    except Exception:
        pass

    from or_client import client
    prompt = (
        f"Today is {datetime.now():%A, %d %B %Y, %H:%M}.\n"
        f"{'About the user:' + chr(10) + profile + chr(10) if profile else ''}"
        f"{'Context from the conversation:' + chr(10) + context + chr(10) if context else ''}"
        f"Question: {question}\n\n"
        "Think it through carefully and get it RIGHT (check maths step by step, consider "
        "trade-offs, be concrete). Then answer for someone who will HEAR it read aloud: "
        "start with the direct answer in one or two sentences, then at most 3 short key "
        "points. No markdown tables, no code blocks unless code was asked for. If something "
        "depends on facts you can't know (prices today, live scores), say so briefly."
    )
    try:
        return client.chat(prompt, system="You are a brilliant, practical expert advisor.",
                           max_tokens=6000, temperature=0.3, smart=True)
    except Exception as e:
        return f"Deep thinking failed: {e}"

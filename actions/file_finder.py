# actions/file_finder.py
#
# "Open my resume", "show me the budget spreadsheet", "open that photo from
# yesterday", "open the snake game folder" — finds files and folders from a
# DESCRIPTION, not an exact path.
#
#   1. A background index of the user's folders (Desktop, Documents,
#      Downloads, Pictures, Videos, Music, OneDrive) — names, folders, dates.
#   2. Fast scoring: words (fuzzy, any order), file-type hints ("spreadsheet",
#      "pdf", "photo"…), recency.
#   3. When it's ambiguous, the fast LLM picks the best of the top candidates
#      given the user's full description.
#   4. Opens it with its default app (or reveals it in Explorer).

from __future__ import annotations

import difflib
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env", ".idea", ".vscode",
    "site-packages", "dist", "build", ".cache", "cache", "temp", "tmp", "$recycle.bin",
    "appdata", ".gradle", ".m2", ".nuget", "obj", "bin", ".next", ".pytest_cache",
}
MAX_ENTRIES   = 250_000
REFRESH_SECS  = 600

TYPE_HINTS = {
    "pdf": {".pdf"},
    "document": {".docx", ".doc", ".pdf", ".odt", ".rtf", ".txt", ".md"},
    "doc": {".docx", ".doc"}, "word": {".docx", ".doc"},
    "spreadsheet": {".xlsx", ".xls", ".csv", ".ods"}, "excel": {".xlsx", ".xls", ".csv"},
    "sheet": {".xlsx", ".xls", ".csv", ".ods"}, "csv": {".csv"},
    "presentation": {".pptx", ".ppt", ".key", ".odp"}, "powerpoint": {".pptx", ".ppt"},
    "slides": {".pptx", ".ppt"}, "deck": {".pptx", ".ppt", ".pdf"},
    "photo": {".jpg", ".jpeg", ".png", ".heic", ".webp"}, "picture": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}, "screenshot": {".png", ".jpg"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm"}, "clip": {".mp4", ".mov", ".mkv", ".webm"},
    "song": {".mp3", ".wav", ".flac", ".m4a", ".ogg"}, "audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "music": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "zip": {".zip", ".rar", ".7z"}, "archive": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "script": {".py", ".js", ".ts", ".ps1", ".bat", ".sh"}, "python": {".py"},
    "code": {".py", ".js", ".ts", ".html", ".css", ".java", ".cpp", ".cs", ".go", ".rs"},
    "model": {".stl", ".3mf", ".obj", ".step", ".stp", ".gcode"},
    "stl": {".stl"}, "3mf": {".3mf"}, "print": {".3mf", ".gcode", ".stl"},
    "text": {".txt", ".md"}, "note": {".txt", ".md", ".docx"}, "notes": {".txt", ".md", ".docx"},
    "installer": {".exe", ".msi"}, "setup": {".exe", ".msi"},
}
FOLDER_WORDS = {"folder", "directory", "dir"}
USER_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".txt", ".md",
             ".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".mp4", ".mov", ".mkv", ".mp3",
             ".wav", ".m4a", ".3mf", ".stl", ".gcode", ".zip", ".odt", ".ods", ".rtf", ".psd"}
NOISY_EXTS = {".vim", ".dll", ".pyc", ".pyd", ".so", ".json", ".xml", ".ini", ".cfg", ".h", ".hpp",
              ".c", ".lua", ".mo", ".po", ".log", ".dat", ".bin", ".lib", ".a", ".tmp", ".lnk",
              ".manifest", ".mui", ".cat", ".inf", ".sys", ".tlb", ".ttf", ".otf", ".map", ".pdb"}
NOISY_PATH_SEGMENTS = tuple(f"\\{seg}\\" for seg in (
    "usr", "share", "lib", "locale", "resources", "include", "vendor", "third_party", "licenses"))
STOPWORDS = {"the", "a", "an", "my", "file", "files", "open", "show", "me", "find", "that",
             "this", "of", "for", "from", "with", "on", "in", "called", "named", "please", "one",
             "up", "pull", "bring", "i", "made", "was", "is", "it", "about", "last", "recent",
             "latest", "newest", "yesterday", "today", "week"} | FOLDER_WORDS | (set(TYPE_HINTS) - {"screenshot", "installer", "setup", "model"})


def _roots() -> list[Path]:
    home = Path.home()
    cands = [home / "Desktop", home / "Documents", home / "Downloads", home / "Pictures",
             home / "Videos", home / "Music"]
    od = os.environ.get("OneDrive")
    if od:
        cands.append(Path(od))
    seen, out = set(), []
    for c in cands:
        try:
            r = c.resolve()
        except Exception:
            continue
        if r.exists() and str(r).lower() not in seen:
            seen.add(str(r).lower())
            out.append(r)
    return out


class _Index:
    def __init__(self):
        self.entries: list[tuple[str, str, bool, float]] = []   # (name_lower, path, is_dir, mtime)
        self.feats: list[tuple] = []        # (name_words, ext, parent_words, joined, noisy_path)
        self.vocab: set[str] = set()        # every distinct word in any file name
        self.exts: set[str] = set()
        self.built_at = 0.0
        self._lock = threading.Lock()
        self._building = False

    def ensure(self, wait: bool = True):
        if self.entries and time.time() - self.built_at < REFRESH_SECS:
            return
        if self.entries and not wait:
            self._refresh_async()
            return
        if not self.entries:
            self._build()
        else:
            self._refresh_async()

    def _refresh_async(self):
        if not self._building:
            threading.Thread(target=self._build, daemon=True, name="FileIndex").start()

    def _build(self):
        if self._building:
            while self._building:
                time.sleep(0.1)
            return
        self._building = True
        t0 = time.time()
        out = []
        try:
            for root in _roots():
                stack = [root]
                while stack and len(out) < MAX_ENTRIES:
                    d = stack.pop()
                    try:
                        with os.scandir(d) as it:
                            for e in it:
                                name = e.name
                                if name.startswith((".", "~$")) or name.lower() in ("desktop.ini", "thumbs.db"):
                                    continue
                                try:
                                    is_dir = e.is_dir(follow_symlinks=False)
                                    mt = e.stat(follow_symlinks=False).st_mtime
                                except OSError:
                                    continue
                                out.append((name.lower(), e.path, is_dir, mt))
                                if is_dir and name.lower() not in SKIP_DIRS:
                                    stack.append(e.path)
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
            feats, vocab = [], set()
            for name, path, is_dir, mt in out:
                stem, ext = os.path.splitext(name)
                if is_dir:
                    stem, ext = name, ""
                words = tuple(re.findall(r"[a-z0-9]+", stem))
                parent = tuple(re.findall(r"[a-z0-9]+", os.path.basename(os.path.dirname(path)).lower()))
                low = path.lower()
                noisy = any(seg in low for seg in NOISY_PATH_SEGMENTS)
                feats.append((words, ext, parent, "".join(words), noisy))
                vocab.update(words)
            with self._lock:
                self.entries = out
                self.feats = feats
                self.vocab = vocab
                self.exts = {f[1] for f in feats if f[1]}
                self.built_at = time.time()
            print(f"[Files] indexed {len(out)} items in {time.time() - t0:.1f}s")
        finally:
            self._building = False


_INDEX = _Index()


def warm_cache():
    threading.Thread(target=_INDEX.ensure, daemon=True, name="FileIndexWarm").start()


def _word_sims(qw: str, vocab: set[str]) -> dict[str, float]:
    """How well query word `qw` matches each filename word — computed ONCE
    against the vocabulary instead of once per file (the slow part)."""
    sims = {qw: 1.0}
    if qw == "screenshot":
        sims.update({"bildschirmfoto": 1.0, "screenshots": 1.0})
    if len(qw) >= 3:
        for w in vocab:
            if w != qw and len(w) >= 3 and (w.startswith(qw) or qw.startswith(w)):
                sims[w] = max(sims.get(w, 0), 0.85)
    if len(qw) >= 4:
        cands = [w for w in vocab if abs(len(w) - len(qw)) <= 3 and w[:1] == qw[:1]]
        for w in difflib.get_close_matches(qw, cands, n=40, cutoff=0.7):
            sims[w] = max(sims.get(w, 0), difflib.SequenceMatcher(None, qw, w).ratio() * 0.8)
    return sims


def _score(sims: list[dict[str, float]], q_joined: str, exts: set[str], want_dir: bool,
           feat: tuple, is_dir: bool, mtime: float, recency_bias: bool) -> float:
    words, ext, parent, joined, noisy = feat
    if want_dir and not is_dir:
        return 0.0
    if exts and not is_dir and ext not in exts:
        return 0.0
    if exts and is_dir and not want_dir:
        return 0.0
    if not sims:
        base = 0.4
    else:
        total = 0.0
        for sm in sims:
            best = 0.0
            for nw in words:
                v = sm.get(nw)
                if v and v > best:
                    best = v
                    if best >= 1.0:
                        break
            if best < 0.55:
                for pw in parent:     # folder name context counts a bit
                    if pw in sm and sm[pw] >= 1.0:
                        best = 0.55
                        break
            total += best
        base = total / len(sims)
        if len(q_joined) >= 4 and q_joined in joined:
            base = max(base, 0.95)
    if base < 0.5:
        return base
    age_days = max(0.0, (time.time() - mtime) / 86400)
    rec = 1.0 / (1.0 + age_days / (3 if recency_bias else 60))
    score = base + 0.15 * rec + (0.05 if is_dir == want_dir else 0)
    # People mean their documents/media, not files buried inside tools.
    if not exts:
        if ext in USER_EXTS:
            score += 0.08
        elif not is_dir and ext in NOISY_EXTS:
            score *= 0.6
    if noisy:
        score *= 0.7
    return score


_EXPAND_CACHE: dict[str, list[str]] = {}


def _expand(description: str) -> list[str]:
    """Other words the file might be NAMED with — synonyms, abbreviations and
    German/English translations ("my CV" → resume, lebenslauf…)."""
    key = description.lower().strip()
    if key in _EXPAND_CACHE:
        return _EXPAND_CACHE[key]
    out: list[str] = []
    try:
        from or_client import client
        ans = client.chat(
            f"A user wants to open a file/folder they described as: \"{description}\".\n"
            "List up to 6 short alternative search terms the FILE NAME might contain: synonyms, "
            "abbreviations spelled out, and German AND English translations. Keep file-type words "
            "(pdf, photo, folder…) out. One term per line, no numbering, no explanations.",
            system="You generate filename search keywords.", max_tokens=60, temperature=0.0)
        for line in ans.splitlines():
            t = re.sub(r"^[\s\-*\d.)]+", "", line).strip().strip('"').lower()
            if 2 <= len(t) <= 40 and t != key:
                out.append(t)
    except Exception as e:
        print(f"[Files] ⚠️ expansion failed: {e}")
    _EXPAND_CACHE[key] = out[:6]
    return _EXPAND_CACHE[key]


def search(description: str, limit: int = 15, expand: bool = False) -> list[dict]:
    """Searches with the user's words, and (expand=True) with AI-suggested
    synonyms/translations too; keeps each path's best score."""
    queries = [(description, 1.0)]
    if expand:
        type_words = " ".join(w for w in re.findall(r"[a-z0-9]+", description.lower())
                              if w in TYPE_HINTS or w in FOLDER_WORDS or w in
                              ("latest", "newest", "recent", "last", "yesterday", "today"))
        queries += [((t + " " + type_words).strip(), 0.97) for t in _expand(description)]
    best: dict[str, dict] = {}
    for q, w in queries:
        for c in _search_one(q, limit=limit):
            c["score"] = round(c["score"] * w, 3)
            if c["path"] not in best or c["score"] > best[c["path"]]["score"]:
                best[c["path"]] = c
    return sorted(best.values(), key=lambda c: -c["score"])[:limit]


def _search_one(description: str, limit: int = 15) -> list[dict]:
    _INDEX.ensure(wait=True)
    with _INDEX._lock:
        entries, feats, vocab, known_exts = _INDEX.entries, _INDEX.feats, _INDEX.vocab, _INDEX.exts
    d = description.lower()
    words = re.findall(r"[a-z0-9]+", d)
    exts: set[str] = set()
    for w in words:
        if w in TYPE_HINTS:
            exts |= TYPE_HINTS[w]
    for e in re.findall(r"\.([a-z0-9]{2,5})", d):
        exts.add("." + e)
    # a bare extension as a word: "prompt txt", "claude md", "the 3mf"
    ext_words = [w for w in words if ("." + w) in known_exts and w not in TYPE_HINTS
                 and w in ("txt", "md", "pdf", "docx", "xlsx", "csv", "pptx", "png", "jpg", "jpeg", "mp4",
                           "mp3", "3mf", "stl", "gcode", "zip", "py", "json", "exe", "mov", "wav", "gif")]
    for w in ext_words:
        exts.add("." + w)
    want_dir = any(w in FOLDER_WORDS for w in words)
    recency = any(w in ("last", "recent", "latest", "newest", "yesterday", "today", "week") for w in words)
    q = [w for w in words if w not in STOPWORDS and w not in ext_words]
    sims = [_word_sims(w, vocab) for w in q]
    q_joined = "".join(q)

    scored = []
    for (name, path, is_dir, mt), feat in zip(entries, feats):
        sc = _score(sims, q_joined, exts, want_dir, feat, is_dir, mt, recency)
        if sc >= 0.5:
            scored.append((sc, path, is_dir, mt))
    if not q and (exts or recency):   # "my latest pdf"
        now = time.time()
        scored = [(0.5 + 0.5 / (1 + (now - mt) / 86400), p, isd, mt)
                  for (name, p, isd, mt), feat in zip(entries, feats)
                  if (not exts or feat[1] in exts) and (isd == want_dir) and not feat[4]]
    scored.sort(key=lambda x: -x[0])
    return [{"score": round(sc, 3), "path": p, "is_dir": isd,
             "modified": datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")}
            for sc, p, isd, mt in scored[:limit]]


def _ai_pick(description: str, cands: list[dict]) -> dict | None:
    try:
        from or_client import client
    except Exception:
        return None
    home = str(Path.home())
    lines = [f"{i + 1}. {c['path'].replace(home, '~')}  ({'folder' if c['is_dir'] else 'file'}, modified {c['modified']})"
             for i, c in enumerate(cands)]
    try:
        ans = client.chat(
            f"The user said: \"{description}\" (today is {datetime.now():%Y-%m-%d}).\n"
            "Candidates on their PC:\n" + "\n".join(lines) + "\n\n"
            "Which ONE do they mean? Prefer the user's own documents/media over files inside "
            "program or tool folders. Reply with just its number, or 0 if none fits.",
            system="You pick the file a user is referring to. Reply with a single number.",
            max_tokens=10, temperature=0.0,
        )
        n = int(re.findall(r"\d+", ans)[0])
        return cands[n - 1] if 1 <= n <= len(cands) else None
    except Exception as e:
        print(f"[Files] ⚠️ AI pick failed: {e}")
        return None


def _is_clear(cands: list[dict]) -> bool:
    if not cands:
        return False
    top = cands[0]
    if top["score"] < 1.05:
        return False
    if len(cands) == 1:
        return True
    second = cands[1]
    # identical names in two places (e.g. a copy in Downloads) — newest wins, no need to ask
    if os.path.basename(top["path"]).lower() == os.path.basename(second["path"]).lower():
        return True
    return top["score"] - second["score"] >= 0.12


def resolve(description: str) -> tuple[dict | None, list[dict]]:
    p = Path(os.path.expandvars(os.path.expanduser(description.strip().strip('"'))))
    if p.is_absolute() and p.exists():
        return {"path": str(p), "is_dir": p.is_dir(), "score": 1.0, "modified": ""}, []
    cands = search(description)
    if _is_clear(cands):
        return _newest_of_twins(cands), cands
    cands = search(description, expand=True)   # widen with synonyms / translations
    if not cands:
        return None, []
    if _is_clear(cands):
        return _newest_of_twins(cands), cands
    return _ai_pick(description, cands[:12]), cands


def _newest_of_twins(cands: list[dict]) -> dict:
    top = cands[0]
    twins = [c for c in cands if os.path.basename(c["path"]).lower() == os.path.basename(top["path"]).lower()
             and abs(c["score"] - top["score"]) < 0.05]
    return max(twins, key=lambda c: c["modified"]) if twins else top


def _open(path: str, reveal: bool = False):
    if reveal:
        subprocess.Popen(["explorer.exe", "/select,", path])
    else:
        os.startfile(path)


def open_file(parameters=None, player=None, **_) -> str:
    params = parameters or {}
    desc   = str(params.get("description") or params.get("query") or params.get("name") or "").strip()
    action = str(params.get("action") or "open").lower()
    if not desc:
        return "Tell me which file or folder — a name or a description is enough."

    if action == "find":
        cands = search(desc, limit=8, expand=True)
        if not cands:
            return f"No files matching '{desc}' in Desktop, Documents, Downloads, Pictures, Videos, Music or OneDrive."
        return "Matches:\n" + "\n".join(f"- {c['path']} (modified {c['modified']})" for c in cands)

    best, cands = resolve(desc)
    if not best:
        if cands:
            alts = "; ".join(os.path.basename(c["path"]) for c in cands[:4])
            return f"Not sure which file '{desc}' means. Closest: {alts}. Ask the user which one."
        return (f"Couldn't find anything matching '{desc}' in Desktop, Documents, Downloads, "
                f"Pictures, Videos, Music or OneDrive.")
    try:
        _open(best["path"], reveal=(action == "reveal"))
    except Exception as e:
        return f"Found {best['path']} but couldn't open it: {e}"
    if player:
        try:
            player.write_log(f"[open_file] {best['path']}")
        except Exception:
            pass
    kind = "folder" if best["is_dir"] else "file"
    verb = "Showed" if action == "reveal" else "Opened"
    return f"{verb} the {kind} {os.path.basename(best['path'])} ({best['path']})."

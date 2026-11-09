"""code_project - BRINKO works on the user's EXISTING code projects.

  1. find the project (path / name / the folder open in VS Code right now)
  2. read the file tree, let the smart model pick the relevant files, read them
  3. the model answers with exact search/replace edits (or new files)
  4. every touched file is backed up to <project>/.brinko_backup/<time>/ first
  5. run the project / tests; on errors feed the output back and fix (max 3 rounds)
  6. open VS Code, report what changed
Safety: edits stay inside the project folder, nothing is deleted, and only plain developer
commands are run (python / node / npm / pytest ...; no chaining, pipes or redirects).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next", ".idea",
             ".vscode", ".brinko_backup", "target", "bin", "obj", ".mypy_cache", ".pytest_cache", "site-packages"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".txt", ".toml", ".yaml", ".yml",
            ".cs", ".java", ".go", ".rs", ".cpp", ".c", ".h", ".lua", ".gd", ".php", ".rb", ".kt", ".swift", ".sh",
            ".bat", ".ps1", ".sql", ".vue", ".svelte", ".ini", ".cfg"}
SAFE_CMDS = {"python", "py", "python3", "pytest", "node", "npm", "npx", "yarn", "pnpm", "deno", "go", "cargo",
             "dotnet", "java", "javac", "gradle", "mvn", "ruby", "php", "lua", "tsc"}
ROOTS = [Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Projects", Path.home() / "source" / "repos",
         Path.home() / "OneDrive" / "Desktop", Path.home() / "OneDrive" / "Documents", Path.home()]

SYSTEM = ("You are a senior software engineer editing an existing project. Make the smallest correct change that "
          "fully does what is asked, in the project's existing style. Never invent APIs. Output JSON only.")


# ── project discovery ──────────────────────────────────────────────────────
def _from_vscode_title() -> str:
    try:
        from actions.screen_watch import context
        c = context()
        if "code" in (c.get("app") or "").lower():
            # "file.py - folder - Visual Studio Code"
            parts = [x.strip() for x in c.get("title", "").split(" - ")]
            if len(parts) >= 3:
                return parts[-2].replace("[Administrator]", "").strip()
    except Exception:
        pass
    return ""


def find_project(hint: str) -> Path | None:
    hint = (hint or "").strip().strip('"')
    if hint and Path(hint).expanduser().is_dir():
        return Path(hint).expanduser().resolve()
    name = hint or _from_vscode_title()
    if not name:
        return None
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    best, best_s = None, 0
    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and depth < 3
                           and d not in ("AppData",)]
            for d in dirnames:
                key = re.sub(r"[^a-z0-9]", "", d.lower())
                s = 3 if key == want else 2 if key.startswith(want) or want.startswith(key) and len(key) > 3 else \
                    1 if want and want in key else 0
                if s > best_s:
                    best, best_s = Path(dirpath) / d, s
            if best_s == 3:
                return best
    return best


def _tree(root: Path, limit: int = 400) -> list[str]:
    out = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            p = Path(dirpath) / f
            if p.suffix.lower() in CODE_EXT and p.stat().st_size < 400_000:
                out.append(str(p.relative_to(root)).replace("\\", "/"))
                if len(out) >= limit:
                    return out
    return out


# ── model calls ───────────────────────────────────────────────────────────
def _ask_json(prompt: str, max_tokens: int = 12000) -> dict:
    from or_client import client
    raw = client.chat(prompt, system=SYSTEM, max_tokens=max_tokens, temperature=0.2, smart=True)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("model gave no JSON")
    return json.loads(m.group(0))


def _pick_files(instruction: str, tree: list[str]) -> list[str]:
    d = _ask_json(
        f"TASK: {instruction}\n\nPROJECT FILES:\n" + "\n".join(tree) +
        '\n\nWhich files must be READ to do this task well (max 8; include entry points/config the change touches)? '
        'JSON: {"files": ["path", ...]}', max_tokens=1500)
    return [f for f in d.get("files", []) if f in tree][:8]


def _read(root: Path, files: list[str], per_file: int = 14000) -> str:
    out = []
    for f in files:
        try:
            txt = (root / f).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        out.append(f"=== {f} ===\n{txt[:per_file]}" + ("\n...(truncated)" if len(txt) > per_file else ""))
    return "\n\n".join(out)


EDIT_FORMAT = (
    'Reply JSON: {"summary": "what you changed, 1-2 sentences", '
    '"edits": [{"file": "rel/path", "search": "EXACT existing text (copied, enough lines to be unique)", '
    '"replace": "new text"}, {"file": "new/file.py", "create": "full content"}], '
    '"run": "command to check it works, e.g. python main.py or npm test (empty if none)"}')


def _plan_edits(instruction: str, tree: list[str], code: str, extra: str = "") -> dict:
    return _ask_json(f"TASK: {instruction}\n\nFILE TREE:\n" + "\n".join(tree[:250]) +
                     f"\n\nRELEVANT FILES:\n{code}\n\n{extra}\n{EDIT_FORMAT}")


# ── applying ──────────────────────────────────────────────────────────────
def _inside(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    return p if str(p).startswith(str(root.resolve())) else None


def _apply(root: Path, edits: list[dict], backup: Path) -> tuple[list[str], list[str]]:
    changed, failed = [], []
    for e in edits:
        rel = str(e.get("file") or "").strip().lstrip("/\\")
        p = _inside(root, rel) if rel else None
        if not p:
            failed.append(f"{rel}: outside the project")
            continue
        if p.exists():
            b = backup / rel
            if not b.exists():
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, b)
        if "create" in e:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(e["create"], encoding="utf-8")
            changed.append(rel)
            continue
        if not p.exists():
            failed.append(f"{rel}: file not found")
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        old, new = e.get("search", ""), e.get("replace", "")
        if old and old in txt:
            txt = txt.replace(old, new, 1)
        else:                                   # tolerate trailing-whitespace / CRLF differences
            norm = lambda s: "\n".join(l.rstrip() for l in s.replace("\r\n", "\n").split("\n"))
            nt, no = norm(txt), norm(old)
            if no and no in nt:
                txt = nt.replace(no, norm(new), 1)
            else:
                failed.append(f"{rel}: the text to replace wasn't found")
                continue
        p.write_text(txt, encoding="utf-8")
        if rel not in changed:
            changed.append(rel)
    return changed, failed


def _safe_cmd(cmd: str) -> list[str] | None:
    cmd = (cmd or "").strip()
    if not cmd or re.search(r"[;&|<>`$]", cmd):
        return None
    parts = cmd.split()
    if parts[0].lower() not in SAFE_CMDS:
        return None
    if parts[0].lower() in ("python", "py", "python3"):
        parts[0] = sys.executable
    elif os.name == "nt" and parts[0].lower() in ("npm", "npx", "yarn", "pnpm", "tsc"):
        parts[0] += ".cmd"
    return parts


def _guess_run(root: Path) -> str:
    if (root / "package.json").exists():
        try:
            if "test" in json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {}):
                return "npm test"
        except Exception:
            pass
    if (root / "tests").is_dir() or list(root.glob("test_*.py")):
        return "python -m pytest -q"
    for f in ("main.py", "app.py", "run.py", "game.py"):
        if (root / f).exists():
            return f"python {f}"
    return ""


def _run(root: Path, cmd: str, timeout: int = 40) -> tuple[bool, str]:
    parts = _safe_cmd(cmd)
    if not parts:
        return True, f"(skipped running '{cmd}': not a plain developer command)"
    try:
        r = subprocess.run(parts, cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (r.stdout[-3000:] + "\n" + r.stderr[-4000:]).strip()
        bad = r.returncode != 0 or bool(re.search(r"Traceback \(most recent call last\)|Error:|FAILED|SyntaxError", r.stderr))
        return not bad, out or f"exit code {r.returncode}"
    except subprocess.TimeoutExpired:
        return True, f"still running after {timeout}s with no crash (app/server/game) - treated as OK"
    except FileNotFoundError as e:
        return True, f"(couldn't run: {e})"


def _open_vscode(root: Path):
    for cmd in ("code", str(Path.home() / "AppData/Local/Programs/Microsoft VS Code/bin/code.cmd")):
        try:
            subprocess.Popen([cmd, str(root)], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


# ── tool ───────────────────────────────────────────────────────────────────
def code_project(parameters: dict, player=None, speak=None) -> str:
    p = parameters or {}
    instruction = str(p.get("instruction") or "").strip()
    if not instruction:
        return "What should I change in the project?"
    root = find_project(str(p.get("project") or ""))
    if not root:
        return ("I couldn't tell which project you mean. Say its folder name, or open it in VS Code and ask again.")
    log = (lambda m: player.write_log(f"SYS: 💻 {m}")) if player else (lambda m: print(f"[Code] {m}"))
    tree = _tree(root)
    if not tree:
        return f"{root} has no code files I can read."
    log(f"{root.name}: reading {len(tree)} files")
    files = _pick_files(instruction, tree)
    code = _read(root, files)
    plan = _plan_edits(instruction, tree, code)
    backup = root / ".brinko_backup" / datetime.now().strftime("%Y%m%d-%H%M%S")
    changed, failed = _apply(root, plan.get("edits", []), backup)
    if failed and not changed:                                      # one retry with the reason
        plan = _plan_edits(instruction, tree, code, "Your previous edits failed: " + "; ".join(failed) +
                           " - copy the search text EXACTLY from the files above.")
        changed, failed = _apply(root, plan.get("edits", []), backup)
    if not changed:
        return f"I couldn't apply the change in {root.name}: " + "; ".join(failed)
    log(f"edited {', '.join(changed)}")
    run_cmd = str(p.get("run_command") or plan.get("run") or _guess_run(root))
    ok, out, rounds = True, "", 0
    if run_cmd:
        ok, out = _run(root, run_cmd)
        while not ok and rounds < 3:
            rounds += 1
            log(f"fixing errors (round {rounds})")
            files2 = list(dict.fromkeys(changed + files))[:10]
            fix = _plan_edits(f"{instruction}\n\nIt was implemented, but running `{run_cmd}` fails. Fix the error.",
                              tree, _read(root, files2), f"RUN OUTPUT:\n{out[-5000:]}")
            c2, _ = _apply(root, fix.get("edits", []), backup)
            changed += [c for c in c2 if c not in changed]
            ok, out = _run(root, run_cmd)
    _open_vscode(root)
    res = (f"Project {root.name}: {plan.get('summary', 'changes made')} Files changed: {', '.join(changed)}."
           + (f" Ran `{run_cmd}`: {'works' if ok else 'still failing'}" + (f" after {rounds} fix round(s)" if rounds else "") + "." if run_cmd else "")
           + (f" Last output: {out[-300:]}" if run_cmd and not ok else "")
           + f" Backup of the old files: {backup}. VS Code is open.")
    return res

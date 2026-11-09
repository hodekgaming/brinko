<p align="center"><img src="assets/brinko.png" width="140" alt="BRINKO"></p>

<h1 align="center">BRINKO</h1>
<p align="center"><b>A voice AI assistant for your Windows PC.</b><br>
Talk to it like a person: it hears you, sees your screen, and does things on your computer.</p>

---

## Install

You only need a **free Gemini API key**: get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

### Easiest: let Claude do it
Open **Claude Code** (or the **Code** tab in the Claude desktop app) and paste:

```
Install BRINKO on my PC from https://github.com/hodekgaming/brinko
```

Claude reads the instructions below and sets everything up. In the normal Claude chat on the web, Claude can't install
things on your PC; it will give you the one-line command below instead.

### Or: one line in PowerShell
Press <kbd>Win</kbd>, type **PowerShell**, open it and paste:

```powershell
irm https://raw.githubusercontent.com/hodekgaming/brinko/main/install.ps1 | iex
```

This installs Python 3.11 and Git if you don't have them, downloads BRINKO to `%USERPROFILE%\Brinko`, installs its
packages, puts a **BRINKO** icon on your desktop and starts it. On first start BRINKO asks for your Gemini key.
Run the same line again later to update.

### First start
1. Paste your Gemini key into the welcome screen (the OpenRouter key is an optional backup; leave it empty).
2. Allow microphone access if Windows asks.
3. Say **"Hey Brinko"**, or press <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>B</kbd>, and talk.

The ⚙ button in the top corner opens **Settings**: voice, email, phone app and more.

---

## What it can do

| | |
|---|---|
| 🎙️ **Voice** | Real-time conversation (Gemini Live), natural female voice by default; pick another voice in Settings or say "change your voice to Aoede" |
| 🖥️ **Your PC** | Open apps and files ("open my CV"), volume, brightness, windows, screenshots, lock / restart / shut down |
| 👀 **Sees your screen** | "What's on my screen?", "help me with this error" |
| ✉️ **Writing** | Emails and messages in your tone (WhatsApp, Telegram, Instagram, Gmail), Word / PDF documents built live on screen |
| 🌐 **Web** | Search, current news, weather, flights, YouTube summaries, music |
| 👨‍💻 **Code** | Write, fix and run code; build small projects |
| 🧠 **Memory** | Remembers your name, people, projects and preferences (stored only on your PC) |
| 🪄 **Camera mode** | Say "camera mode": hold both hands up, thumb and index finger spread, and the space between your fingertips becomes a window into another look (Spider-Verse, Anime, Comic, Synthwave, Hologram, Thermal). Pull your hands wide to fill the screen. |
| 🌌 **Screensaver** | A real-time black hole |
| 📱 **Phone app** | Settings → Connect phone: talk to BRINKO from your Android phone |

---

## Requirements
- Windows 10 or 11 (macOS / Linux: partly works, not supported)
- Python 3.11 or 3.12 (the installer gets it for you)
- A microphone; a webcam for camera mode
- Optional: `ffmpeg` for some audio / video file features

## Privacy
Your keys, memory, logs and logins stay in `config/`, `memory/` and `logs/` on your PC and are never uploaded to this
repository (see `.gitignore`). Voice and requests go to Google's Gemini API with **your** key.

## Manual install
```powershell
git clone https://github.com/hodekgaming/brinko.git Brinko
cd Brinko
py -3.11 -m pip install -r requirements.txt
py -3.11 -m pip install pycaw comtypes win10toast pywinauto
py -3.11 -m playwright install chromium
py -3.11 make_desktop_shortcut.py
py -3.11 main.py
```

---

## For Claude / AI assistants: how to install BRINKO for the user

If a user gave you this repository's link and asked you to install it, do this on their Windows PC:

1. Check for Python 3.11 or 3.12 (`py -3.11 --version`, then `py -3.12 --version`). If neither exists, install it with
   `winget install -e --id Python.Python.3.11 --scope user`, then reopen the shell or refresh `PATH`.
2. Check for Git (`git --version`); if missing, `winget install -e --id Git.Git`.
3. Clone into the user's home folder: `git clone --depth 1 https://github.com/hodekgaming/brinko.git "%USERPROFILE%\Brinko"`
   (if it already exists, `git -C "%USERPROFILE%\Brinko" pull` instead).
4. In that folder, with that Python:
   `-m pip install -r requirements.txt`, then `-m pip install pycaw comtypes win10toast pywinauto`,
   then `-m playwright install chromium`, then run `make_desktop_shortcut.py`.
5. Ask the user for their Gemini API key (free at https://aistudio.google.com/apikey). Do **not** type it into chat
   output or commit it. Either let them paste it into BRINKO's welcome screen on first start, or, if they give it to
   you, write `config/api_keys.json` as `{"gemini_api_key": "<key>", "os_system": "windows"}`.
6. Start it without a console: `pythonw.exe main.py` from that folder (or the new desktop icon). Tell the user to say
   "Hey Brinko" or press Ctrl+Alt+B.

Downloads are about 500 MB in total (Python packages + Chromium). Instead of steps 1–4 and 6 you can also run
`install.ps1` from this repo.

---

## Credits & license
BRINKO is built on **MARK XXV**, a JARVIS-style voice assistant from the open-source "Jarvis Mark" series by
[FatihMakes](https://www.youtube.com/@FatihMakes).

Licensed under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): free to use, share and change
for **non-commercial** purposes with credit. See [LICENSE](LICENSE).

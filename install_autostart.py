"""
install_autostart.py — run this ONCE to make Brinko launch automatically
whenever you log into your computer, so it's "awake" the moment you sit
down instead of needing to be started by hand.

Usage:
    python install_autostart.py            # install
    python install_autostart.py --remove   # uninstall

What it does per OS:
    Windows : drops a .vbs launcher (silent, no console flash) into the
              Startup folder (shell:startup).
    macOS   : installs a LaunchAgent plist that runs on login.
    Linux   : installs a .desktop autostart entry (XDG spec).

It always launches `python main.py` from THIS project's directory, so move
this file with the project (don't run it from a copy elsewhere).
"""

import sys
import platform
import argparse
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
MAIN_PY     = PROJECT_DIR / "main.py"
APP_NAME    = "BRINKO"
_OS         = platform.system()


def _windows_install():
    import os
    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    vbs_path    = startup_dir / f"{APP_NAME}.vbs"

    pythonw = sys.executable
    if pythonw.lower().endswith("python.exe"):
        candidate = pythonw[:-len("python.exe")] + "pythonw.exe"
        if Path(candidate).exists():
            pythonw = candidate

    vbs_content = (
        'Set objShell = CreateObject("WScript.Shell")\n'
        f'objShell.Run """{pythonw}"" ""{MAIN_PY}""", 0, False\n'
    )
    vbs_path.write_text(vbs_content, encoding="utf-8")
    print(f"✅ Installed. BRINKO will launch silently on login.\n   {vbs_path}")


def _windows_remove():
    import os
    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    vbs_path = startup_dir / f"{APP_NAME}.vbs"
    if vbs_path.exists():
        vbs_path.unlink()
        print("✅ Removed autostart entry.")
    else:
        print("ℹ️ No autostart entry found.")


def _macos_install():
    plist_dir  = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"com.brinko.app.plist"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.brinko.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{MAIN_PY}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>WorkingDirectory</key><string>{PROJECT_DIR}</string>
    <key>StandardOutPath</key><string>/tmp/brinko.log</string>
    <key>StandardErrorPath</key><string>/tmp/brinko.err.log</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    print(f"✅ Installed. Run: launchctl load {plist_path}\n   (or just log out/in — it'll pick it up automatically)")


def _macos_remove():
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.brinko.app.plist"
    if plist_path.exists():
        import subprocess
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        print("✅ Removed autostart entry.")
    else:
        print("ℹ️ No autostart entry found.")


def _linux_install():
    autostart_dir = Path.home() / ".config" / "autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    desktop_path = autostart_dir / f"{APP_NAME}.desktop"

    desktop_content = f"""[Desktop Entry]
Type=Application
Name=BRINKO
Exec={sys.executable} {MAIN_PY}
Path={PROJECT_DIR}
X-GNOME-Autostart-enabled=true
NoDisplay=false
"""
    desktop_path.write_text(desktop_content, encoding="utf-8")
    print(f"✅ Installed. BRINKO will launch on next login.\n   {desktop_path}")


def _linux_remove():
    desktop_path = Path.home() / ".config" / "autostart" / f"{APP_NAME}.desktop"
    if desktop_path.exists():
        desktop_path.unlink()
        print("✅ Removed autostart entry.")
    else:
        print("ℹ️ No autostart entry found.")


def main():
    parser = argparse.ArgumentParser(description="Install/remove Brinko autostart-on-login.")
    parser.add_argument("--remove", action="store_true", help="Uninstall autostart instead of installing it.")
    args = parser.parse_args()

    if not MAIN_PY.exists():
        print(f"⚠️ main.py not found next to this script ({MAIN_PY}). "
              f"Move install_autostart.py into the project root first.")
        return

    handlers = {
        "Windows": (_windows_install, _windows_remove),
        "Darwin":  (_macos_install,   _macos_remove),
        "Linux":   (_linux_install,   _linux_remove),
    }

    if _OS not in handlers:
        print(f"⚠️ Unsupported OS: {_OS}")
        return

    install_fn, remove_fn = handlers[_OS]
    (remove_fn if args.remove else install_fn)()


if __name__ == "__main__":
    main()

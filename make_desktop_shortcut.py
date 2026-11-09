# make_desktop_shortcut.py — puts a "BRINKO" icon on your desktop.
#
#   python make_desktop_shortcut.py
#
# Double-clicking the icon starts BRINKO with no terminal window (pythonw.exe).
# Logs go to logs/brinko.log. Launching it again while BRINKO is already running
# just brings the existing window to the front.

import os
import subprocess
import sys
from pathlib import Path

HERE      = Path(__file__).resolve().parent
ICON_PATH = HERE / "assets" / "brinko.ico"


def make_icon(path: Path = ICON_PATH) -> Path:
    """Draws BRINKO's look as a multi-size .ico: a white-hot star with a blue halo and
    lens flares on a royal-blue disc with a few constellation stars (the Nov 2026 HUD)."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    S = 1024                      # drawn big, scaled down for the .ico sizes
    c = S / 2
    y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    r = np.hypot(x - c, y - c) / (S * 0.47)          # 1.0 = disc edge

    # royal-blue disc: radial gradient #1c3fd0 -> #040b37, slightly lit from the centre
    t = np.clip(r, 0, 1)[..., None]
    inner, outer = np.array([28, 63, 208], np.float32), np.array([4, 11, 55], np.float32)
    rgb = inner * (1 - t ** 1.3) + outer * (t ** 1.3)

    # star: halo glow + white core + horizontal / vertical lens flares
    d = np.hypot(x - c, y - c) / S
    halo = np.exp(-(d / 0.16) ** 2) * 0.85
    glow = np.exp(-(d / 0.055) ** 2)
    core = np.clip(1.25 - d / 0.045, 0, 1)
    fh = np.exp(-((y - c) / (S * 0.006)) ** 2) * np.exp(-np.abs(x - c) / (S * 0.20))
    fv = np.exp(-((x - c) / (S * 0.005)) ** 2) * np.exp(-np.abs(y - c) / (S * 0.09)) * 0.7
    blue = np.array([127, 214, 255], np.float32)
    rgb = rgb + halo[..., None] * (blue - rgb) * 0.75
    rgb = rgb + glow[..., None] * (np.array([200, 236, 255], np.float32) - rgb)
    flare = np.clip(fh + fv, 0, 1)[..., None]
    rgb = rgb + flare * (np.array([225, 243, 255], np.float32) - rgb)
    rgb = rgb + core[..., None] * (255 - rgb)

    a = np.clip((1.0 - r) * S * 0.47 / 2.0, 0, 1) * 255     # anti-aliased disc edge
    img = Image.fromarray(np.dstack([np.clip(rgb, 0, 255), a]).astype(np.uint8), "RGBA")

    # a few constellation stars + faint lines, outside the halo
    pts = [(0.24, 0.30), (0.33, 0.22), (0.30, 0.40), (0.72, 0.26), (0.80, 0.36), (0.68, 0.76),
           (0.78, 0.70), (0.26, 0.72), (0.36, 0.80)]
    lines = [(0, 1), (0, 2), (3, 4), (5, 6), (7, 8)]
    lay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    P = [(px * S, py * S) for px, py in pts]
    for i, j in lines:
        ld.line([P[i], P[j]], fill=(150, 200, 255, 90), width=4)
    for (px, py) in P:
        ld.ellipse((px - 9, py - 9, px + 9, py + 9), fill=(220, 238, 255, 230))
    soft = lay.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(soft)
    img.alpha_composite(lay)
    # keep the stars inside the disc
    img.putalpha(Image.fromarray(np.minimum(np.array(img)[..., 3], a).astype(np.uint8)))
    # thin bright rim
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse((c - S * 0.47, c - S * 0.47, c + S * 0.47, c + S * 0.47),
                                outline=(120, 170, 255, 150), width=10)
    img.alpha_composite(rim)

    img = img.resize((256, 256), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(path.with_suffix(".png"))
    return path


def _desktop_dir() -> Path:
    # Handles OneDrive-redirected desktops.
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    p = Path(r.stdout.strip() or Path.home() / "Desktop")
    return p


def make_shortcut() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    icon = make_icon()
    lnk = _desktop_dir() / "BRINKO.lnk"

    def q(s) -> str:
        return str(s).replace("'", "''")

    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath = '{exe}';"
        "$s.Arguments = '\"{main}\"';"
        "$s.WorkingDirectory = '{wd}';"
        "$s.IconLocation = '{ico},0';"
        "$s.Description = 'Start BRINKO, your AI assistant';"
        "$s.WindowStyle = 1;"
        "$s.Save()"
    ).format(lnk=q(lnk), exe=q(pythonw), main=q(HERE / "main.py"), wd=q(HERE), ico=q(icon))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return lnk


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("This script creates a Windows desktop shortcut.")
    print(f"Created {make_shortcut()}")

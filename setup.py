import subprocess
import sys
import platform

print("=" * 55)
print("  BRINKO — Setup")
print("=" * 55)

print("\nInstalling core requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)

system = platform.system()
if system == "Windows":
    print("\nInstalling Windows-specific extras (pycaw, comtypes, win10toast, pywinauto)...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "pycaw", "comtypes", "win10toast", "pywinauto"],
        check=False,  # non-fatal if one fails
    )
else:
    print(f"\nSkipping Windows-only packages (pycaw/comtypes/win10toast/pywinauto) — running on {system}.")

print("\nInstalling Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

print("\n✅ Setup complete!")
print("Before running, make sure:")
print("  1. ffmpeg is installed as a system binary (not just pip) — needed for audio/video features")
print("  2. You have a free Gemini API key (aistudio.google.com) and OpenRouter API key (openrouter.ai)")
print("\nRun 'python main.py' to start BRINKO. A setup screen will ask for your API keys on first launch.")

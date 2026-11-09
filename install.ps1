# BRINKO installer for Windows 10/11.
#
#   One line, in PowerShell:
#   irm https://raw.githubusercontent.com/hodekgaming/brinko/main/install.ps1 | iex
#
# What it does: makes sure Python 3.11 and Git exist (installs them with winget if not),
# downloads BRINKO to %USERPROFILE%\Brinko, installs its Python packages, puts a BRINKO
# icon on the desktop and starts it. On first start BRINKO asks for a free Gemini API key.
# Run it again any time to update.

$ErrorActionPreference = "Stop"
$Repo = "https://github.com/hodekgaming/brinko.git"
$Dir  = if ($env:BRINKO_DIR) { $env:BRINKO_DIR } else { Join-Path $env:USERPROFILE "Brinko" }

function Say($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
    foreach ($v in "3.11", "3.12") {
        try { $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    }
    try {
        $p = & python -c "import sys; print(sys.executable) if sys.version_info[:2] in ((3,11),(3,12)) else None" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p -and $p.Trim() -ne "None") { return $p.Trim() }
    } catch {}
    return $null
}

Say "Checking Python 3.11 / 3.12"
$py = Find-Python
if (-not $py) {
    Say "Installing Python 3.11 (winget)"
    winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python
    if (-not $py) { throw "Python 3.11 was installed but can't be found yet. Close this window, open a new PowerShell and run the installer again." }
}
Write-Host "Python: $py"

Say "Checking Git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

if (Test-Path (Join-Path $Dir ".git")) {
    Say "Updating BRINKO in $Dir"
    git -C $Dir pull --ff-only
} else {
    Say "Downloading BRINKO to $Dir"
    git clone --depth 1 $Repo $Dir
}

Say "Installing Python packages (takes a few minutes the first time)"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r (Join-Path $Dir "requirements.txt")
& $py -m pip install pycaw comtypes win10toast pywinauto
Say "Installing the browser BRINKO uses for web tasks"
& $py -m playwright install chromium

Say "Making the desktop icon"
Push-Location $Dir
& $py make_desktop_shortcut.py
Pop-Location

Say "Starting BRINKO"
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
Start-Process -FilePath $pyw -ArgumentList "`"$(Join-Path $Dir 'main.py')`"" -WorkingDirectory $Dir

Write-Host ""
Write-Host "BRINKO is installed in $Dir and starting now." -ForegroundColor Green
Write-Host "It will ask for a free Gemini API key: get one at https://aistudio.google.com/apikey"
Write-Host "Next time, just double-click the BRINKO icon on your desktop."

# Build Focus Tool into a single .exe.
# - Creates the Python environment OUTSIDE Box (in %LOCALAPPDATA%\FocusTool\venv) if missing.
# - Installs pinned dependencies, runs the tests, then runs PyInstaller.
# - Writes to .\build\FocusTool-YYYY-MM-DD-vN.exe, picking N above every build already there,
#   so an earlier build is never overwritten.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$venv = Join-Path $env:LOCALAPPDATA "FocusTool\venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment at $venv"
    python -m venv $venv
}
& $py -m pip install --disable-pip-version-check -q -r requirements.txt

Write-Host "Running tests"
$env:PYTHONDONTWRITEBYTECODE = "1"  # keep __pycache__ and .pytest_cache out of the Box folder
& $py -m pytest -q -p no:cacheprovider tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed; not building." }

$work = Join-Path $env:LOCALAPPDATA "FocusTool\pyinstaller-work"
$dist = Join-Path $env:LOCALAPPDATA "FocusTool\pyinstaller-dist"
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name FocusTool `
    --icon (Join-Path $here "assets\icon.ico") `
    --add-data ((Join-Path $here "assets") + ";assets") `
    --add-data ((Join-Path $here "settings.default.json") + ";.") `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml --exclude-module PySide6.Qt3DCore `
    --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts --exclude-module PySide6.QtNetwork `
    --workpath $work --distpath $dist --specpath $work `
    (Join-Path $here "focus_tool.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$outDir = Join-Path $here "build"
New-Item -ItemType Directory -Force $outDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd"
# Next number = highest existing vN for today + 1, so a deleted older build never gets its name reused.
$existing = Get-ChildItem $outDir -Filter ("FocusTool-{0}-v*.exe" -f $stamp) -ErrorAction SilentlyContinue |
    ForEach-Object { if ($_.BaseName -match "-v(\d+)$") { [int]$Matches[1] } }
$n = 1
if ($existing) { $n = ($existing | Measure-Object -Maximum).Maximum + 1 }
$target = Join-Path $outDir ("FocusTool-{0}-v{1}.exe" -f $stamp, $n)
while (Test-Path $target) { $n++; $target = Join-Path $outDir ("FocusTool-{0}-v{1}.exe" -f $stamp, $n) }
Copy-Item (Join-Path $dist "FocusTool.exe") $target
$size = [math]::Round((Get-Item $target).Length / 1MB, 1)
Write-Host "Built $target ($size MB)"

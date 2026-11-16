# Creates the environment and downloads the voice. Safe to run again.
# The environment lives under %LOCALAPPDATA%\invoke-bridge because deep
# project paths break Python virtual environments on Windows.
$ErrorActionPreference = 'Stop'
$root = "$env:LOCALAPPDATA\invoke-bridge"
New-Item -ItemType Directory -Force "$root\voices" | Out-Null
if (-not (Test-Path "$root\venv\Scripts\python.exe")) { python -m venv "$root\venv" }
& "$root\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$root\venv\Scripts\python.exe" -m pip install --quiet -r "$PSScriptRoot\requirements.txt"
if (-not (Test-Path "$root\voices\en_US-amy-medium.onnx")) {
    & "$root\venv\Scripts\python.exe" -m piper.download_voices en_US-amy-medium --data-dir "$root\voices"
}
"Ready. Start the bridge with .\run.ps1"

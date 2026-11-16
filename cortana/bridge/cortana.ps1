# Starts the speaker link. The bridge (run.ps1) must already be running.
# Arguments pass through, e.g. .\cortana.ps1 --levels 8   or   .\cortana.ps1 --say "hello"
& "$env:LOCALAPPDATA\invoke-bridge\venv\Scripts\python.exe" -u "$PSScriptRoot\satellite.py" @args

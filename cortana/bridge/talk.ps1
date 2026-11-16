# Push-to-talk test client. Arguments pass through, e.g. .\talk.ps1 --devices
& "$env:LOCALAPPDATA\invoke-bridge\venv\Scripts\python.exe" "$PSScriptRoot\talk.py" @args

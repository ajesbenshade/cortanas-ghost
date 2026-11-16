# Waits for the speaker to show up in service mode (Marvell BG2CD, 1286:8174),
# then hands it to WSL and keeps re-attaching it if it re-enumerates.
# WSL must already be running, with the loader started first.
$usbipd = "$env:ProgramFiles\usbipd-win\usbipd.exe"
$deadline = (Get-Date).AddMinutes(45)
"waiting for 1286:8174 ..."
while ((Get-Date) -lt $deadline) {
    $line = & $usbipd list 2>$null | Select-String '^\S+\s+1286:8174' | Select-Object -First 1
    if ($line) {
        $busid = ($line.Line -split '\s+')[0]
        "found on bus $busid at $(Get-Date -Format T); attaching"
        & $usbipd attach --wsl --busid $busid --auto-attach
        exit $LASTEXITCODE
    }
    Start-Sleep -Milliseconds 500
}
"gave up waiting"
exit 1

# Start-BluetoothAudioReceiver.ps1
# Launch the installed Bluetooth Audio Receiver UWP app so the phone can
# stream audio into the PC as a standing policy.

param(
    [int]$DelaySeconds = 35
)

$AppId = "55746MarkSmirnov.BluetoothAudioReveicer_xwrbx6997tsfc!App"

if ($DelaySeconds -gt 0) {
    Start-Sleep -Seconds $DelaySeconds
}

Start-Process "explorer.exe" "shell:AppsFolder\$AppId"

# Open the Bluetooth Audio Receiver playback connection once the UWP window is ready.

param(
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$lastInvokeError = $null
$buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
    "OpenAudioPlaybackConnectionButtonButton"
)

while ((Get-Date) -lt $deadline) {
    $process = Get-Process ApplicationFrameHost -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq "Bluetooth Audio Receiver" -and $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1

    if ($process) {
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
            $button = $root.FindFirst([System.Windows.Automation.TreeScope]::Subtree, $buttonCondition)
            if ($button) {
                $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                $pattern.Invoke()
                exit 0
            }
        } catch {
            # Window handles and the button state can change while the app settles.
            $lastInvokeError = $_.Exception.Message
        }
    }

    Start-Sleep -Milliseconds 500
}

if ($lastInvokeError) {
    Write-Error "Timed out opening Bluetooth Audio Receiver connection: $lastInvokeError"
} else {
    Write-Error "Timed out opening Bluetooth Audio Receiver connection."
}
exit 1

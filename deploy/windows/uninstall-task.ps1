param([string]$TaskName = "QwenAutonomousSupervisor")

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task: $TaskName"
} else {
    Write-Output "Scheduled task not found: $TaskName"
}


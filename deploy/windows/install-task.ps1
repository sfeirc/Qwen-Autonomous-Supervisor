param(
    [Parameter(Mandatory = $true)]
    [string]$QasExecutable,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$TaskName = "QwenAutonomousSupervisor",
    [string]$RunAsUser = $env:USERNAME
)

$ErrorActionPreference = "Stop"
$qas = (Resolve-Path -LiteralPath $QasExecutable).Path
$config = (Resolve-Path -LiteralPath $ConfigPath).Path
$workingDirectory = Split-Path -Parent $config

$action = New-ScheduledTaskAction `
    -Execute $qas `
    -Argument "--config `"$config`" loop" `
    -WorkingDirectory $workingDirectory
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId $RunAsUser `
    -LogonType S4U `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Persistent crash-restarting Qwen Autonomous Supervisor" `
    -Force | Out-Null

Write-Output "Installed scheduled task: $TaskName"


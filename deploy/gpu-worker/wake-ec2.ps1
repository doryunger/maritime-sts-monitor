param(
    [Parameter(Mandatory = $true)]
    [uri]$ControllerUrl,
    [Parameter(Mandatory = $true)]
    [System.Net.IPAddress]$WorkerPrivateIp,
    [int]$PollSeconds = 20
)

$ErrorActionPreference = "Stop"
$statusUri = [uri]::new($ControllerUrl, "/_wake/status")
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$controlConfigPath = Join-Path $repoRoot "data\secrets\control_host.json"
if (-not (Test-Path -LiteralPath $controlConfigPath)) {
    throw "Lightsail connection settings were not found at $controlConfigPath"
}
$control = Get-Content -LiteralPath $controlConfigPath -Raw | ConvertFrom-Json
$controlKey = [Environment]::ExpandEnvironmentVariables($control.identity_file)
$controlTarget = "{0}@{1}" -f $control.user, $control.host
$ssh = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
if (-not $ssh) {
    $ssh = (Get-Command ssh -ErrorAction Stop).Source
}

$remoteCommand = "chmod 600 ~/.ssh/geogen-key.pem && ssh -t -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i ~/.ssh/geogen-key.pem ubuntu@$WorkerPrivateIp"

$sshProcess = $null
try {
    while ($true) {
        $status = Invoke-RestMethod -Uri $statusUri -Method Get -TimeoutSec 15
        Write-Host ("{0} EC2={1} stage={2}" -f (Get-Date -Format "HH:mm:ss"), $status.ec2_state, $status.stage)
        if ($status.stage -eq "error") {
            throw "Wake controller reported an error: $($status.detail)"
        }
        if ($status.stage -eq "ready") {
            break
        }
        Start-Sleep -Seconds $PollSeconds
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $ssh
    $startInfo.UseShellExecute = $false
    $startInfo.Arguments = "-t -i `"$controlKey`" -o BatchMode=yes -o StrictHostKeyChecking=accept-new $controlTarget `"$remoteCommand`""
    $sshProcess = [System.Diagnostics.Process]::Start($startInfo)
    while (-not $sshProcess.HasExited) {
        Start-Sleep -Seconds $PollSeconds
        $status = Invoke-RestMethod -Uri $statusUri -Method Get -TimeoutSec 15
        Write-Host ("{0} EC2={1} stage={2}" -f (Get-Date -Format "HH:mm:ss"), $status.ec2_state, $status.stage)
    }
    exit $sshProcess.ExitCode
}
finally {
    if ($sshProcess -and -not $sshProcess.HasExited) {
        $sshProcess.Kill($true)
    }
}

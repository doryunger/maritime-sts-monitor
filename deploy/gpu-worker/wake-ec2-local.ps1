param(
    [Parameter(Mandatory = $true)]
    [string]$WorkerPrivateIp,
    [string]$WorkerKeyPath = (Join-Path $env:USERPROFILE "Downloads\geogen-key.pem"),
    [int]$TimeoutSeconds = 1200
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "wake-ec2-local.py"
if (-not (Test-Path -LiteralPath $python)) {
    throw "The repository Python environment was not found at $python"
}
& $python $script --worker-private-ip $WorkerPrivateIp --key-path $WorkerKeyPath --timeout-seconds $TimeoutSeconds
exit $LASTEXITCODE

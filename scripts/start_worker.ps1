#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DataDirectory,
    [Parameter(Mandatory)][string]$UpstreamDirectory,
    [switch]$AllowDownloads
)
$ErrorActionPreference = 'Stop'
$DataRoot = [IO.Path]::GetFullPath($DataDirectory)
$BridgePython = Join-Path $DataRoot 'mcp-env\Scripts\python.exe'
$RuntimePython = Join-Path $DataRoot 'runtime\Scripts\python.exe'
foreach ($Executable in @($BridgePython, $RuntimePython)) {
    if (-not (Test-Path -LiteralPath $Executable)) { throw "Python not found: $Executable" }
}
$Heartbeat = Join-Path $DataRoot 'worker-service.json'
if (Test-Path -LiteralPath $Heartbeat) {
    $State = Get-Content -LiteralPath $Heartbeat -Raw | ConvertFrom-Json
    if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $State.updated -lt 15) {
        Write-Host "Worker is already running (PID $($State.pid))."
        return
    }
}
$env:KIMODO_REPO = [IO.Path]::GetFullPath($UpstreamDirectory)
$env:KIMODO_PYTHON = $RuntimePython
$env:KIMODO_DATA_DIR = $DataRoot
$env:KIMODO_WORKER_MODE = 'service'
$env:HF_HOME = Join-Path $DataRoot 'cache\huggingface'
$env:TEXT_ENCODER_DEVICE = 'cpu'
$env:KIMODO_ALLOW_DOWNLOADS = if ($AllowDownloads) { '1' } else { '0' }
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$Process = Start-Process -FilePath $BridgePython -ArgumentList @('-m', 'kimodo_mcp.worker_service') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $DataRoot 'worker-service.log') -RedirectStandardError (Join-Path $DataRoot 'worker-service-error.log')
for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    if ($Process.HasExited) { throw 'Worker exited during startup. Read worker-service-error.log.' }
    if (Test-Path -LiteralPath $Heartbeat) {
        $State = Get-Content -LiteralPath $Heartbeat -Raw | ConvertFrom-Json
        if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $State.updated -lt 15) {
            Write-Host "Worker ready (PID $($State.pid)). Jobs continue when MCP clients close."
            return
        }
    }
    Start-Sleep -Milliseconds 250
}
throw 'Worker has not published its heartbeat. Read worker-service-error.log.'

#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DataDirectory,
    [string]$UpstreamDirectory,
    [ValidateRange(1024, 65535)][int]$Port = 8768,
    [switch]$Build,
    [switch]$StartWorker,
    [switch]$OpenBrowser
)
$ErrorActionPreference = 'Stop'
$DataRoot = [IO.Path]::GetFullPath($DataDirectory)
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$BridgePython = Join-Path $DataRoot 'mcp-env\Scripts\python.exe'
$RuntimePython = Join-Path $DataRoot 'runtime\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $BridgePython)) { throw "Bridge Python not found: $BridgePython" }
$WebRoot = Join-Path $RepositoryRoot 'web'
$IndexFile = Join-Path $WebRoot 'dist\index.html'
if ($Build -or -not (Test-Path -LiteralPath $IndexFile)) {
    Push-Location -LiteralPath $WebRoot
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw 'Studio dependency installation failed' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Studio build failed' }
    } finally { Pop-Location }
}
$env:KIMODO_DATA_DIR = $DataRoot
$env:KIMODO_PYTHON = $RuntimePython
$env:KIMODO_WORKER_MODE = 'service'
$env:HF_HOME = Join-Path $DataRoot 'cache\huggingface'
$env:TEXT_ENCODER_DEVICE = 'cpu'
if ($UpstreamDirectory) { $env:KIMODO_REPO = [IO.Path]::GetFullPath($UpstreamDirectory) }
if ($StartWorker) {
    if (-not $env:KIMODO_REPO) { throw 'Provide UpstreamDirectory to start the worker' }
    & (Join-Path $PSScriptRoot 'start_worker.ps1') -DataDirectory $DataRoot -UpstreamDirectory $env:KIMODO_REPO
}
$StudioUrl = "http://127.0.0.1:$Port"
try {
    $ExistingStudio = Invoke-RestMethod "$StudioUrl/api/state" -TimeoutSec 2
    if ($ExistingStudio.health.server -eq 'ready' -and $ExistingStudio.csrf_token) {
        if ($ExistingStudio.health.data_directory -ne $DataRoot) { throw 'This port serves another data directory' }
        Write-Host "Studio is already running at $StudioUrl"
        if ($OpenBrowser) { Start-Process $StudioUrl -WindowStyle Hidden }
        return
    }
} catch [System.Net.Http.HttpRequestException] { }
catch [System.Threading.Tasks.TaskCanceledException] { }
$Process = Start-Process -FilePath $BridgePython -ArgumentList @('-m', 'kimodo_mcp.studio', '--port', "$Port", '--web-directory', 'web/dist') -WorkingDirectory $RepositoryRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $DataRoot 'studio-stdout.log') -RedirectStandardError (Join-Path $DataRoot 'studio-stderr.log')
for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    if ($Process.HasExited) { throw 'Studio exited during startup. Read studio-stderr.log.' }
    try {
        $ReadyStudio = Invoke-RestMethod "$StudioUrl/api/state" -TimeoutSec 2
        if ($ReadyStudio.health.server -eq 'ready') {
            Write-Host "Studio ready at $StudioUrl"
            $Process.Id | Set-Content -LiteralPath (Join-Path $DataRoot 'studio.pid')
            if ($OpenBrowser) { Start-Process $StudioUrl -WindowStyle Hidden }
            return
        }
    } catch [System.Net.Http.HttpRequestException] { }
    catch [System.Threading.Tasks.TaskCanceledException] { }
    Start-Sleep -Milliseconds 250
}
throw 'Studio did not become ready. Read studio-stderr.log.'

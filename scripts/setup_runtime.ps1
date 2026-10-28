#Requires -Version 7.0
<#
.SYNOPSIS
Install separate Windows environments for the MCP bridge and Kimodo inference.
.DESCRIPTION
Downloads Python packages and a pinned Kimodo checkout. Model weights are not
downloaded. Existing source checkouts must already match the reviewed revision;
this script never resets local work or edits assistant configuration files.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DataDirectory,

    [string]$UpstreamDirectory,

    [string]$PythonVersion = '3.12',

    [string]$TorchVersion = '2.11.0',

    [switch]$NativeDependencyFallback
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$UpstreamRevision = 'e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954'
$UpstreamUrl = 'https://github.com/nv-tlabs/kimodo.git'
$BridgeDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$DataRoot = [System.IO.Path]::GetFullPath($DataDirectory)
if (-not $UpstreamDirectory) {
    $UpstreamDirectory = Join-Path $DataRoot 'upstream-kimodo'
}
$UpstreamRoot = [System.IO.Path]::GetFullPath($UpstreamDirectory)

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

foreach ($ToolName in @('uv', 'git', 'cmake')) {
    if (-not (Get-Command $ToolName -ErrorAction SilentlyContinue)) {
        throw "Install $ToolName and make it available in PATH before running setup."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $BridgeDirectory 'pyproject.toml'))) {
    throw 'Run this script from a complete kimodo-mcp checkout.'
}
if ($TorchVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw 'TorchVersion must be a stable version such as 2.11.0.'
}

New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
if (Test-Path -LiteralPath $UpstreamRoot) {
    if (-not (Test-Path -LiteralPath (Join-Path $UpstreamRoot '.git'))) {
        throw 'UpstreamDirectory exists but is not a Git checkout. Choose a new directory.'
    }
    $InstalledRevision = & git -c "safe.directory=$($UpstreamRoot.Replace('\', '/'))" -C $UpstreamRoot rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $InstalledRevision.Trim() -ne $UpstreamRevision) {
        throw "Existing upstream checkout must be at $UpstreamRevision. Choose a new directory to preserve local work."
    }
    $LocalChanges = & git -c "safe.directory=$($UpstreamRoot.Replace('\', '/'))" -C $UpstreamRoot status --porcelain
    if ($LASTEXITCODE -ne 0 -or $LocalChanges) {
        throw 'Existing upstream checkout has local changes. Choose a new directory or review them first.'
    }
} else {
    Invoke-Checked -Command 'git' -Arguments @('clone', '--no-checkout', $UpstreamUrl, $UpstreamRoot)
    Invoke-Checked -Command 'git' -Arguments @('-c', "safe.directory=$($UpstreamRoot.Replace('\', '/'))", '-C', $UpstreamRoot, 'checkout', '--detach', $UpstreamRevision)
}

$BridgeEnvironment = Join-Path $DataRoot 'mcp-env'
$RuntimeEnvironment = Join-Path $DataRoot 'runtime'
$BridgePython = Join-Path $BridgeEnvironment 'Scripts\python.exe'
$RuntimePython = Join-Path $RuntimeEnvironment 'Scripts\python.exe'
foreach ($EnvironmentPath in @($BridgeEnvironment, $RuntimeEnvironment)) {
    if (-not (Test-Path -LiteralPath (Join-Path $EnvironmentPath 'Scripts\python.exe'))) {
        if (Test-Path -LiteralPath $EnvironmentPath) {
            throw "Environment directory exists without Python: $EnvironmentPath"
        }
        Invoke-Checked -Command 'uv' -Arguments @('venv', '--python', $PythonVersion, $EnvironmentPath)
    }
}

Write-Host 'Installing the MCP bridge environment...'
Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $BridgePython, '-e', "${BridgeDirectory}[dev]")
Write-Host 'Installing CUDA 12.8 PyTorch into the inference environment...'
Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $RuntimePython, "torch==$TorchVersion", '--index-url', 'https://download.pytorch.org/whl/cu128')
Write-Host 'Building Kimodo and its native motion-correction extension...'
try {
    Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $RuntimePython, '-e', $UpstreamRoot)
} catch {
    if (-not $NativeDependencyFallback) {
        throw "Kimodo installation failed. If CMake could not fetch pybind11 or Eigen, rerun with -NativeDependencyFallback. Original error: $_"
    }
    Write-Host 'Installing explicit native dependencies after the normal build failed...'
    Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $RuntimePython, 'pybind11==2.11.1', 'setuptools', 'wheel')
    $DependenciesRoot = Join-Path $DataRoot 'build-deps'
    $EigenSource = Join-Path $DependenciesRoot 'eigen'
    $EigenBuild = Join-Path $DependenciesRoot 'eigen-build'
    $EigenPrefix = Join-Path $DependenciesRoot 'eigen-install'
    $EigenRevision = '3147391d946bb4b6c68edd901f2add6ac1f31f8c'
    if (-not (Test-Path -LiteralPath $EigenSource)) {
        Invoke-Checked -Command 'git' -Arguments @('clone', '--depth', '1', '--branch', '3.4.0', 'https://gitlab.com/libeigen/eigen.git', $EigenSource)
    }
    $EigenSafe = "safe.directory=$($EigenSource.Replace('\', '/'))"
    $EigenHead = & git -c $EigenSafe -C $EigenSource rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $EigenHead.Trim() -ne $EigenRevision) {
        throw 'Existing Eigen checkout differs from the pinned 3.4.0 source. Review it before continuing.'
    }
    $EigenChanges = & git -c $EigenSafe -C $EigenSource status --porcelain
    if ($LASTEXITCODE -ne 0 -or $EigenChanges) {
        throw 'Existing Eigen checkout has local changes. Review it before continuing.'
    }
    Invoke-Checked -Command 'cmake' -Arguments @('-S', $EigenSource, '-B', $EigenBuild, "-DCMAKE_INSTALL_PREFIX=$EigenPrefix", '-DEIGEN_BUILD_DOC=OFF', '-DBUILD_TESTING=OFF')
    Invoke-Checked -Command 'cmake' -Arguments @('--install', $EigenBuild, '--config', 'Release')
    $PybindPrefix = & $RuntimePython -m pybind11 --cmakedir
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the installed pybind11 CMake configuration.' }
    $PreviousPrefix = $env:CMAKE_PREFIX_PATH
    $PreviousSkip = $env:SKIP_MOTION_CORRECTION_IN_SETUP
    try {
        $env:CMAKE_PREFIX_PATH = "$($PybindPrefix.Trim());$EigenPrefix"
        if ($PreviousPrefix) { $env:CMAKE_PREFIX_PATH += ";$PreviousPrefix" }
        Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $RuntimePython, '--no-build-isolation', (Join-Path $UpstreamRoot 'MotionCorrection'))
        $env:SKIP_MOTION_CORRECTION_IN_SETUP = '1'
        Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $RuntimePython, '-e', $UpstreamRoot)
    } finally {
        $env:CMAKE_PREFIX_PATH = $PreviousPrefix
        $env:SKIP_MOTION_CORRECTION_IN_SETUP = $PreviousSkip
    }
}
Invoke-Checked -Command 'uv' -Arguments @('pip', 'install', '--python', $RuntimePython, '--no-deps', '-e', $BridgeDirectory)

$CacheRoot = Join-Path $DataRoot 'cache\huggingface'
New-Item -ItemType Directory -Path $CacheRoot -Force | Out-Null
$env:KIMODO_REPO = $UpstreamRoot
$env:KIMODO_PYTHON = $RuntimePython
$env:KIMODO_DATA_DIR = $DataRoot
$env:TEXT_ENCODER_DEVICE = 'cpu'
$env:HF_HOME = $CacheRoot

Write-Host 'Checking local runtime imports without downloading model weights...'
Invoke-Checked -Command $RuntimePython -Arguments @('-m', 'kimodo_mcp.worker', '--probe')

Write-Host ''
Write-Host "Bridge Python: $BridgePython"
Write-Host "Kimodo Python: $RuntimePython"
Write-Host "Kimodo source: $UpstreamRoot"
Write-Host "Data directory: $DataRoot"
Write-Host "Hugging Face cache: $CacheRoot"
Write-Host 'Environment values apply to this PowerShell session. Add them to your MCP client configuration.'
Write-Host 'For first-run models, sign in with Hugging Face, obtain gated encoder access, then explicitly set KIMODO_ALLOW_DOWNLOADS=1.'
Write-Host 'See docs/clients.md for client registration. Package installation does not verify successful model inference.'
Write-Host 'Start the independent generation worker once (restart after reboot or configuration changes):'
Write-Host ".\scripts\start_worker.ps1 -DataDirectory `"$DataRoot`" -UpstreamDirectory `"$UpstreamRoot`""
Write-Host 'Add -AllowDownloads only when enabling authorized first-run model downloads.'

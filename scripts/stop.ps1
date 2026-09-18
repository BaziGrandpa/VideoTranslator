param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$targetIds = [System.Collections.Generic.HashSet[int]]::new()

function Add-LauncherProcess {
    param([int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -match '(?i)-m\s+app\.launcher') {
        [void]$targetIds.Add($ProcessId)
    }
}

$pidFile = Join-Path $projectRoot "data\video-translator.pid"
if (Test-Path -LiteralPath $pidFile) {
    $storedId = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($storedId -match '^\d+$') {
        Add-LauncherProcess -ProcessId ([int]$storedId)
    }
}

$connections = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(6866, 8000) }
foreach ($connection in $connections) {
    Add-LauncherProcess -ProcessId $connection.OwningProcess
}

if ($targetIds.Count -eq 0) {
    if (-not $DryRun) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Video Translator is not running."
    exit 0
}

foreach ($processId in $targetIds) {
    if ($DryRun) {
        Write-Host "Would stop Video Translator process tree $processId."
        continue
    }

    Write-Host "Stopping Video Translator process tree $processId..."
    & taskkill.exe /PID $processId /T /F
    if ($LASTEXITCODE -ne 0) {
        throw "taskkill failed for process $processId with exit code $LASTEXITCODE"
    }
}

if (-not $DryRun) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Video Translator stopped."
}

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Warning "Codex CLI was not found in PATH. Install/login before translating."
}
if (-not (Get-Command grok -ErrorAction SilentlyContinue)) {
    Write-Warning "Grok Build CLI was not found in PATH. Codex can still be used without fallback."
}

Write-Host ""
Write-Host "Setup complete. Make sure Codex CLI is logged in; Grok CLI is the fallback."
Write-Host "Start the app with: .\.venv\Scripts\python.exe -m app.launcher"

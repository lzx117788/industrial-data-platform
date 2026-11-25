param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
uv run --frozen python -m app.cli serve --port $Port

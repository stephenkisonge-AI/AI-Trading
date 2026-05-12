# Launches the Alpaca MCP server with credentials loaded from the project-local .env file.
# Registered with Claude Code so API keys live only in .env, never in Claude Code's config.

$ErrorActionPreference = 'Stop'

$envPath = Join-Path $PSScriptRoot '..\.env' | Resolve-Path

if (-not (Test-Path $envPath)) {
    Write-Error ".env not found at $envPath"
    exit 1
}

& "$env:USERPROFILE\.local\bin\uvx.exe" --from alpaca-mcp-server alpaca-mcp-server --env-file "$envPath"

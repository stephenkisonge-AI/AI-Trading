# Loads .env from the project root and launches the Alpaca MCP server.
# Registered with Claude Code so keys live only in .env, never in Claude Code's config.

$ErrorActionPreference = 'Stop'

$envPath = Join-Path $PSScriptRoot '..\.env' | Resolve-Path
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)\s*$') {
        $name  = $matches[1]
        $value = $matches[2].Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

if (-not $env:ALPACA_API_KEY -or -not $env:ALPACA_SECRET_KEY) {
    Write-Error "ALPACA_API_KEY or ALPACA_SECRET_KEY missing from $envPath"
    exit 1
}

& "$env:USERPROFILE\.local\bin\uvx.exe" alpaca-mcp-server serve

#requires -Version 5.1
<#
.SYNOPSIS
  Run the grounded-chat LIVE evaluation against the running stack (#29).

.DESCRIPTION
  Convenience wrapper for the human verification step in
  docs/runbooks/grounded-chat-live-smoke.md §6: it runs the live answer-quality
  eval (real OpenRouter model + real pgvector hybrid over the golden corpus) and
  the live LLM/retrieval smoke tests.

  These need an OPENROUTER_API_KEY and a reachable Postgres; the underlying
  pytest tests SKIP cleanly when either is absent (offline-safe), so this script
  just sets the environment and shells out — it never invents behaviour.

  It does NOT bring the stack up; do that first with `docker compose up -d`
  (ADR-0005). The DATABASE_URL default points at the compose Postgres on the
  471xx host-port block.

.PARAMETER ApiKey
  The OpenRouter API key. Defaults to the existing $env:OPENROUTER_API_KEY.

.PARAMETER DatabaseUrl
  The async Postgres URL. Defaults to the compose-mapped local Postgres.

.EXAMPLE
  pwsh -File .\scripts\run-live-eval.ps1 -ApiKey sk-or-...
#>

[CmdletBinding()]
param(
  [string]$ApiKey = $env:OPENROUTER_API_KEY,
  [string]$DatabaseUrl = $(if ($env:DATABASE_URL) { $env:DATABASE_URL } else { 'postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen' })
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
  Write-Warning 'OPENROUTER_API_KEY is not set. The live eval will SKIP (offline-safe). Pass -ApiKey or set $env:OPENROUTER_API_KEY.'
}

$env:OPENROUTER_API_KEY = $ApiKey
$env:DATABASE_URL = $DatabaseUrl

$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot 'backend'

Write-Host "Running live eval from $backend (DATABASE_URL=$DatabaseUrl)" -ForegroundColor Cyan
Push-Location $backend
try {
  & uv run --extra dev pytest `
    tests/eval/test_eval_live.py `
    tests/test_llm_gateway.py::test_live_openrouter_chat_smoke `
    tests/test_retrieval_service.py::test_live_hybrid_search_is_permission_filtered `
    -v
}
finally {
  Pop-Location
}

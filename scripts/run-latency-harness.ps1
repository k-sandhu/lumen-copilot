#requires -Version 5.1
<#
.SYNOPSIS
  Run the client-perspective chat-latency harness against the running stack (#486).

.DESCRIPTION
  The parent tracking issue #486 must close on numbers, but its own instrumentation
  caveat is that AC-1 (time-to-first-answer-token) and AC-7 (typed terminal error
  from an unreachable provider) cannot be verified without a timing harness. This
  wrapper runs that harness (backend/tests/eval/test_chat_latency_live.py):

    * AC-1 — samples the wall-clock from the `start` envelope to the first answer
      `delta` for a single-tool grounded answer on the FAST default model, and
      reports TTFT p50/p95 (asserts p50 < 2s by default).
    * AC-7 — routes an answer at a black-hole api_base and asserts a typed terminal
      error arrives within the interactive budget (<= 30s), not after ~182s.

  It measures over the REAL Redis backplane when Redis is reachable (so the #487
  pooled-publish path is on the clock), else the in-memory fan-out (the printed
  report names which). Timing is client-perspective: the harness subscribes before
  generation and stamps `time.perf_counter()` at the instant it drains each
  envelope, so TTFAT / total include the publish, the WS relay, and the fan-out a
  browser pays (the server `ts` is kept only as a generation-vs-transport diagnostic).

  The harness is opt-in: it spends real tokens, so it runs only when RUN_LIVE=1 is
  set (plus a reachable key / Postgres / OpenSearch) and otherwise SKIPS cleanly
  (offline-safe, no socket, no tokens). This script sets RUN_LIVE=1 for you so the
  documented one-command path runs live; when the key or a datastore is absent the
  pytest still skips cleanly. It does NOT bring the stack up; do that first with
  `docker compose up -d` (ADR-0005). See docs/runbooks/chat-latency-harness.md.

.PARAMETER ApiKey
  The OpenRouter API key. Defaults to the existing $env:OPENROUTER_API_KEY.

.PARAMETER DatabaseUrl
  The async Postgres URL. Defaults to the compose-mapped local Postgres.

.PARAMETER Samples
  How many AC-1 answers to sample for the p50/p95 (default 5).

.PARAMETER MeasureOnly
  Print the numbers without asserting the AC budgets (LATENCY_ASSERT=0).

.EXAMPLE
  pwsh -File .\scripts\run-latency-harness.ps1 -ApiKey sk-or-...

.EXAMPLE
  # Explore without failing on the budget, with more samples:
  pwsh -File .\scripts\run-latency-harness.ps1 -Samples 10 -MeasureOnly
#>

[CmdletBinding()]
param(
  [string]$ApiKey = $env:OPENROUTER_API_KEY,
  [string]$DatabaseUrl = $(if ($env:DATABASE_URL) { $env:DATABASE_URL } else { 'postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen' }),
  [int]$Samples = 5,
  [switch]$MeasureOnly
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
  Write-Warning 'OPENROUTER_API_KEY is not set. The harness will SKIP (offline-safe). Pass -ApiKey or set $env:OPENROUTER_API_KEY.'
}

# The explicit live opt-in (issue #94 parity): the harness only runs — and only
# then probes datastores / spends tokens — when RUN_LIVE is set. This documented
# wrapper is the "run it live" path, so it sets it; a bare pytest stays offline-safe.
$env:RUN_LIVE = '1'
$env:OPENROUTER_API_KEY = $ApiKey
$env:DATABASE_URL = $DatabaseUrl
$env:LATENCY_SAMPLES = "$Samples"
if ($MeasureOnly) { $env:LATENCY_ASSERT = '0' }

$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot 'backend'

Write-Host "Running chat-latency harness from $backend (DATABASE_URL=$DatabaseUrl, samples=$Samples)" -ForegroundColor Cyan
Push-Location $backend
try {
  # -s so the harness's printed TTFT / error-budget report reaches the console.
  & uv run --extra dev pytest tests/eval/test_chat_latency_live.py -s -v
}
finally {
  Pop-Location
}

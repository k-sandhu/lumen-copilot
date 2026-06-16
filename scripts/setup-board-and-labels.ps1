#requires -Version 5.1
<#
.SYNOPSIS
  Idempotent setup of the GitHub issue-tracking infrastructure for lumen-copilot:
  the base label set + the "Lumen Copilot" GitHub Projects (v2) board.

.DESCRIPTION
  Part of the issue pipeline (see docs/WORK_TRACKING.md). Safe to re-run.
  Creates/updates a small set of STRUCTURAL labels only. The story-derived
  epic:* and persona:* labels are created on demand by stories-to-issues.ps1
  from the finalized user stories, so they stay correct as the stories evolve.

.PARAMETER ProjectTitle
  Title of the Projects board. Default: "Lumen Copilot".

.NOTES
  Requires the gh CLI authenticated with 'repo' and 'project' scopes.

.EXAMPLE
  pwsh ./scripts/setup-board-and-labels.ps1
#>
[CmdletBinding()]
param(
  [string]$ProjectTitle = "Lumen Copilot"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  throw "gh CLI not found. Install GitHub CLI and run 'gh auth login' (scopes: repo, project)."
}

# --- base structural labels (epic:* / persona:* come from stories-to-issues.ps1) ---
$labels = @(
  @{ name = "type:user-story";  color = "1d76db"; description = "A user story (persona -> capability -> benefit)" }
  @{ name = "type:epic";        color = "5319e7"; description = "A grouping of related user stories" }
  @{ name = "type:adr";         color = "0e8a16"; description = "Architecture Decision Record work" }
  @{ name = "type:docs";        color = "0075ca"; description = "Documentation" }
  @{ name = "type:chore";       color = "fef2c0"; description = "Maintenance / tooling" }
  @{ name = "type:bug";         color = "d73a4a"; description = "Something is broken" }
  @{ name = "blocked";          color = "b60205"; description = "Blocked (often on an open decision)" }
  @{ name = "needs-spec";       color = "e99695"; description = "Behavior undefined - write a spec/ADR first" }
  @{ name = "good-first-issue"; color = "7057ff"; description = "Good entry point" }
)

Write-Host "Ensuring base labels..." -ForegroundColor Cyan
foreach ($l in $labels) {
  gh label create $l.name --color $l.color --description $l.description --force | Out-Null
  Write-Host "  - $($l.name)"
}

# --- board ---
Write-Host "Ensuring Projects board '$ProjectTitle'..." -ForegroundColor Cyan
$list = gh project list --owner "@me" --format json | ConvertFrom-Json
$existing = $list.projects | Where-Object { $_.title -eq $ProjectTitle } | Select-Object -First 1
if ($existing) {
  Write-Host "  already exists: #$($existing.number)  $($existing.url)" -ForegroundColor Green
} else {
  $created = gh project create --owner "@me" --title $ProjectTitle --format json | ConvertFrom-Json
  Write-Host "  created: #$($created.number)  $($created.url)" -ForegroundColor Green
  Write-Host "  (New boards ship with a Status field: Todo / In Progress / Done.)"
}

Write-Host ""
Write-Host "Done. Record the board number/URL in docs/WORK_TRACKING.md." -ForegroundColor Green

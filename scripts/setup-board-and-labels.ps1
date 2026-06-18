#requires -Version 5.1
<#
.SYNOPSIS
  Idempotent setup of the GitHub issue-tracking infrastructure for beacon:
  the base label set + the "Beacon" GitHub Projects (v2) board.

.DESCRIPTION
  Part of the issue pipeline (see docs/WORK_TRACKING.md). Safe to re-run.
  Creates/updates the STRUCTURAL label set (work types, areas, sizing, risk,
  severity). The story-derived epic:* and persona:* labels are created on
  demand by stories-to-issues.ps1 from the finalized user stories, so they
  stay correct as the stories evolve.

.PARAMETER ProjectTitle
  Title of the Projects board. Default: "Beacon".

.NOTES
  Requires the gh CLI authenticated with 'repo' and 'project' scopes.

.EXAMPLE
  pwsh ./scripts/setup-board-and-labels.ps1
#>
[CmdletBinding()]
param(
  [string]$ProjectTitle = "Beacon"
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

  # --- work types (operating model: features / cross-cuttings / spikes) ---
  @{ name = "type:feature";       color = "0e8a16"; description = "A deployable capability satisfying 3-7 stories (the unit of claim)" }
  @{ name = "type:cross-cutting"; color = "1d76db"; description = "A foundation many features depend on (claimed first)" }
  @{ name = "type:spike";         color = "fbca04"; description = "A timeboxed investigation whose deliverable is an ADR" }
  @{ name = "needs-adr";          color = "d4c5f9"; description = "Decision undefined - open an ADR first" }

  # --- sizing ---
  @{ name = "size:S"; color = "c2e0c6"; description = "Small" }
  @{ name = "size:M"; color = "fef2c0"; description = "Medium" }
  @{ name = "size:L"; color = "f9d0c4"; description = "Large" }

  # --- review-gate risk ---
  @{ name = "risk:security";      color = "b60205"; description = "Touches auth/permissions - security review required" }
  @{ name = "risk:data";          color = "d93f0b"; description = "Touches data handling/retention - review required" }
  @{ name = "risk:cross-cutting"; color = "e99695"; description = "Affects a shared foundation" }

  # --- QA bug severity ---
  @{ name = "severity:critical"; color = "b60205"; description = "Critical - broken or unsafe" }
  @{ name = "severity:high";     color = "d93f0b"; description = "High" }
  @{ name = "severity:medium";   color = "fbca04"; description = "Medium" }
  @{ name = "severity:low";      color = "c2e0c6"; description = "Low" }

  # --- build-lane areas (consolidated-structure.md section 8) ---
  @{ name = "area:search";             color = "c5def5"; description = "Build lane: search" }
  @{ name = "area:agents";             color = "c5def5"; description = "Build lane: agents" }
  @{ name = "area:connectors";         color = "c5def5"; description = "Build lane: connectors" }
  @{ name = "area:permissions";        color = "c5def5"; description = "Build lane: permissions" }
  @{ name = "area:ingestion";          color = "c5def5"; description = "Build lane: ingestion" }
  @{ name = "area:ui";                 color = "c5def5"; description = "Build lane: ui" }
  @{ name = "area:api";                color = "c5def5"; description = "Build lane: api" }
  @{ name = "area:observability";      color = "c5def5"; description = "Build lane: observability" }
  @{ name = "area:identity";           color = "c5def5"; description = "Build lane: identity" }
  @{ name = "area:model-gateway";      color = "c5def5"; description = "Build lane: model-gateway" }
  @{ name = "area:citations";          color = "c5def5"; description = "Build lane: citations" }
  @{ name = "area:storage";            color = "c5def5"; description = "Build lane: storage" }
  @{ name = "area:proactive";          color = "c5def5"; description = "Build lane: proactive" }
  @{ name = "area:meetings";           color = "c5def5"; description = "Build lane: meetings" }
  @{ name = "area:knowledge-quality";  color = "c5def5"; description = "Build lane: knowledge-quality" }
  @{ name = "area:actions";            color = "c5def5"; description = "Build lane: actions" }
  @{ name = "area:scheduling";         color = "c5def5"; description = "Build lane: scheduling" }
  @{ name = "area:builder";            color = "c5def5"; description = "Build lane: builder" }
  @{ name = "area:research";           color = "c5def5"; description = "Build lane: research" }
  @{ name = "area:artifacts";          color = "c5def5"; description = "Build lane: artifacts" }
  @{ name = "area:department-pack";    color = "c5def5"; description = "Build lane: department-pack" }
  @{ name = "area:developer-platform"; color = "c5def5"; description = "Build lane: developer-platform" }
  @{ name = "area:browser-desktop";    color = "c5def5"; description = "Build lane: browser-desktop" }
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

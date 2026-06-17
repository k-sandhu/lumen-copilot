#requires -Version 5.1
<#
.SYNOPSIS
  External multi-process fan-out for the agent fleet: launch one worker per
  claimable issue, each in its own git worktree + branch.

.DESCRIPTION
  The cross-harness / CI counterpart to in-session subagent fan-out (see
  docs/roles/orchestrator.md). Given an epic (or -All), it finds the claimable
  issues and launches one worker process per issue.

  DRY-RUN BY DEFAULT: prints the launch plan and writes nothing. Pass -Execute
  (with -Runner) to create worktrees and start workers.

  "Claimable" = issue is open and unassigned, and (if the board exposes a
  Status field) its Status equals -Status. The 6-state board with a "Ready"
  status lands in a separate chore; until then pass -Status "Todo".

.PARAMETER Epic
  Epic number; fan out issues labelled epic:<N>. Omit and pass -All for every claimable issue.

.PARAMETER Runner
  Command template to launch one worker. Placeholders: {issue} {url} {branch} {worktree}.
  Confirm the exact flags for your CLI/version, e.g.:
    Claude Code : 'claude -p "/implementer {issue}"'
    Codex       : 'codex exec "/implementer {issue}"'
    OpenCode    : 'opencode run "/implementer {issue}"'
  If omitted, the script only prints the plan (forces dry-run).

.PARAMETER MaxParallel
  Cap on concurrent workers (default 3). At most one worker per area:* at a time.

.PARAMETER Execute
  Actually create worktrees and launch workers. Default is dry-run.

.NOTES
  Requires gh authenticated with 'repo' + 'project' scopes.
  Starting template: the runner integration is pluggable and has NOT yet been
  exercised against a live board with real feature issues.

.EXAMPLE
  pwsh ./scripts/run-fleet.ps1 -Epic 1 -Status Todo
  pwsh ./scripts/run-fleet.ps1 -Epic 1 -Runner 'claude -p "/implementer {issue}"' -Execute
#>
[CmdletBinding()]
param(
  [int]$Epic,
  [switch]$All,
  [string]$Status = "Ready",
  [string]$Runner = "",
  [int]$MaxParallel = 3,
  [int]$Board = 7,
  [string]$Owner = "@me",
  [switch]$Execute
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  throw "gh CLI not found. Install GitHub CLI and run 'gh auth login' (scopes: repo, project)."
}
if (-not $Epic -and -not $All) { throw "Specify -Epic <N> or -All." }
if ($Execute -and -not $Runner) { throw "-Execute needs -Runner '<command template>' (see Get-Help). Without it this is a dry run." }

function Get-Slug([string]$text) {
  $s = $text.ToLower() -replace '[^a-z0-9]+', '-'
  $s = $s.Trim('-')
  if ($s.Length -gt 40) { $s = $s.Substring(0, 40).Trim('-') }
  if (-not $s) { $s = "issue" }
  return $s
}

# 1. Authoritative issue data (labels, assignees, state) from gh issue list.
$listArgs = @("issue", "list", "--state", "open", "--limit", "500", "--json", "number,title,url,labels,assignees")
if ($Epic) { $listArgs += @("--label", "epic:$Epic") }
$issues = & gh @listArgs | ConvertFrom-Json

# 2. Board Status per issue number (best-effort; tolerate absence).
$statusByNum = @{}
try {
  $proj = gh project item-list $Board --owner $Owner --format json --limit 500 | ConvertFrom-Json
  foreach ($it in $proj.items) {
    if ($it.content -and $it.content.number) { $statusByNum[[int]$it.content.number] = $it.status }
  }
} catch {
  Write-Host "  (could not read board status; proceeding on open+unassigned only)" -ForegroundColor Yellow
}

# 3. Filter to claimable candidates.
$candidates = @()
foreach ($iss in $issues) {
  if ($iss.assignees -and $iss.assignees.Count -gt 0) { continue }   # already claimed
  $st = $statusByNum[[int]$iss.number]
  if ($st -and ($st -ne $Status)) { continue }                       # wrong board status
  $labels = @($iss.labels | ForEach-Object { $_.name })
  $area = ($labels | Where-Object { $_ -like "area:*" } | Select-Object -First 1)
  $candidates += [pscustomobject]@{ Number = $iss.number; Title = $iss.title; Url = $iss.url; Area = $area }
}

if (-not $candidates) {
  $scope = if ($Epic) { "epic:$Epic, " } else { "" }
  Write-Host "No claimable items ($scope Status='$Status', open+unassigned). Nothing to launch." -ForegroundColor Yellow
  return
}

# 4. Enforce area-exclusivity + parallelism budget.
$picked = @()
$usedAreas = @{}
foreach ($cand in ($candidates | Sort-Object Number)) {
  if ($picked.Count -ge $MaxParallel) { break }
  if ($cand.Area -and $usedAreas.ContainsKey($cand.Area)) { continue }
  if ($cand.Area) { $usedAreas[$cand.Area] = $true }
  $picked += $cand
}

$mode = if ($Execute) { "EXECUTE" } else { "DRY-RUN" }
Write-Host ""
Write-Host "[$mode] Launching $($picked.Count) of $($candidates.Count) claimable (MaxParallel=$MaxParallel, one per area):" -ForegroundColor Green

foreach ($p in $picked) {
  $branch = "feat/$($p.Number)-$(Get-Slug $p.Title)"
  $wt = Join-Path (Split-Path -Parent (Get-Location)) "fleet-$($p.Number)"
  $cmd = $Runner.Replace("{issue}", "$($p.Number)").Replace("{url}", $p.Url).Replace("{branch}", $branch).Replace("{worktree}", $wt)
  $areaDisp = if ($p.Area) { $p.Area } else { "-" }
  Write-Host ("  #{0,-4} area={1,-20} branch={2}" -f $p.Number, $areaDisp, $branch)
  if ($Execute) {
    git worktree add -b $branch $wt origin/main | Out-Null
    Write-Host "    worktree: $wt"
    Write-Host "    launch:   $cmd"
    Start-Process -FilePath "powershell" -ArgumentList "-NoLogo", "-Command", $cmd -WorkingDirectory $wt
  } else {
    $launchDisp = if ($cmd) { $cmd } else { "(no -Runner supplied; plan only)" }
    Write-Host "    would worktree: $wt"
    Write-Host "    would launch:   $launchDisp"
  }
}

Write-Host ""
Write-Host "Done ($mode)." -ForegroundColor Green
if (-not $Execute) { Write-Host "Re-run with -Runner '<cmd>' -Execute to create worktrees and start workers." }

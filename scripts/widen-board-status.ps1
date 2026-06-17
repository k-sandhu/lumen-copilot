#requires -Version 5.1
<#
.SYNOPSIS
  Idempotently widen the board's Status single-select to the 6-state workflow:
  Backlog -> Ready -> In Progress -> In Review -> Blocked -> Done.

.DESCRIPTION
  Replaces the Status field options via the GitHub GraphQL API. The
  'updateProjectV2Field' mutation RECREATES the options with new ids, which
  BLANKS every item's Status. To stay safe this script snapshots each item's
  current Status (by name) before the mutation and restores it afterward by
  matching the name to the new option id. Items whose old status name does not
  survive (e.g. "Todo") cannot be restored and are reported.

  DRY-RUN BY DEFAULT: prints the resolved field id and the target options.
  Pass -Execute to apply.

.PARAMETER Board
  Project board number. Default: 7.

.PARAMETER Owner
  Project owner login. Default: k-sandhu (avoid the '@me' alias - it can fail to
  resolve intermittently).

.NOTES
  Requires gh authenticated with the 'project' scope.

.EXAMPLE
  pwsh ./scripts/widen-board-status.ps1            # dry run
  pwsh ./scripts/widen-board-status.ps1 -Execute   # apply (snapshots + restores item Status)
#>
[CmdletBinding()]
param(
  [int]$Board = 7,
  [string]$Owner = "k-sandhu",
  [switch]$Execute
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  throw "gh CLI not found. Install GitHub CLI and run 'gh auth login' (scope: project)."
}

# Resolve project id + Status field id.
$projId = (gh project view $Board --owner $Owner --format json | ConvertFrom-Json).id
$fields = gh project field-list $Board --owner $Owner --format json | ConvertFrom-Json
$status = $fields.fields | Where-Object { $_.name -eq "Status" } | Select-Object -First 1
if (-not $status) { throw "No 'Status' field on board #$Board." }
$fieldId = $status.id

# Target options (order matters).
$opts = @(
  @{ n = "Backlog";     c = "GRAY";   d = "Exists, not yet Ready" }
  @{ n = "Ready";       c = "GREEN";  d = "Passes Definition of Ready - claimable" }
  @{ n = "In Progress"; c = "YELLOW"; d = "Claimed, branch open" }
  @{ n = "In Review";   c = "BLUE";   d = "PR open, awaiting Reviewer" }
  @{ n = "Blocked";     c = "RED";    d = "Waiting on a dependency / open decision" }
  @{ n = "Done";        c = "PURPLE"; d = "Merged" }
)

Write-Host "Status field: $fieldId" -ForegroundColor Cyan
Write-Host ("Target: " + (($opts | ForEach-Object { $_.n }) -join " -> "))

if (-not $Execute) {
  Write-Host "DRY-RUN: re-run with -Execute to apply." -ForegroundColor Yellow
  return
}

# Snapshot current item Status (by name) so we can restore after the recreate.
$snapshot = @()
$items = (gh project item-list $Board --owner $Owner --format json | ConvertFrom-Json).items
foreach ($it in $items) {
  if ($it.status) { $snapshot += [pscustomobject]@{ Id = $it.id; Status = $it.status } }
}
Write-Host "Snapshotted $($snapshot.Count) item(s) with a Status."

# Build + run the mutation. Write to a UTF-8 (no BOM) temp file and let gh read
# it (-F @file): passing the multi-line query as a native arg or via stdin trips
# Windows PowerShell 5.1 quoting / BOM handling and corrupts the GraphQL.
$optStr = ($opts | ForEach-Object { '{name:"' + $_.n + '", color:' + $_.c + ', description:"' + $_.d + '"}' }) -join ",`n      "
$query = @"
mutation {
  updateProjectV2Field(input:{
    fieldId:"$fieldId",
    singleSelectOptions:[
      $optStr
    ]
  }){
    projectV2Field{ ... on ProjectV2SingleSelectField{ id name options{ id name } } }
  }
}
"@
$tmp = Join-Path $env:TEMP ("widen-board-" + [guid]::NewGuid().ToString() + ".graphql")
[System.IO.File]::WriteAllText($tmp, $query, (New-Object System.Text.UTF8Encoding($false)))
try {
  $res = gh api graphql -F "query=@$tmp"
  if ($LASTEXITCODE -ne 0) { throw "GraphQL mutation failed (gh exit $LASTEXITCODE)." }
} finally {
  Remove-Item $tmp -ErrorAction SilentlyContinue
}

# Map new option name -> id, then restore each snapshotted item.
$newOpts = ($res | ConvertFrom-Json).data.updateProjectV2Field.projectV2Field.options
$idByName = @{}
foreach ($o in $newOpts) { $idByName[$o.name] = $o.id }
Write-Host ("New options: " + (($newOpts | ForEach-Object { $_.name }) -join " -> ")) -ForegroundColor Green

$restored = 0
$orphaned = @()
foreach ($s in $snapshot) {
  $newId = $idByName[$s.Status]
  if ($newId) {
    gh project item-edit --id $s.Id --field-id $fieldId --project-id $projId --single-select-option-id $newId | Out-Null
    $restored++
  } else {
    $orphaned += $s.Status
  }
}
Write-Host "Restored Status on $restored item(s)." -ForegroundColor Green
if ($orphaned.Count -gt 0) {
  Write-Host ("WARNING: could not restore (status name dropped): " + ($orphaned -join ", ")) -ForegroundColor Yellow
}
Write-Host "Board Status widened to 6 states." -ForegroundColor Green

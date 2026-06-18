#requires -Version 5.1
<#
.SYNOPSIS
  Turn the user-stories markdown into GitHub issues (one per story), grouped by
  epic (milestone) and tagged by persona, added to the Projects board.

.DESCRIPTION
  Idempotent and DRY-RUN BY DEFAULT. Re-running skips any story whose issue
  already exists (matched by the "[E?-?]" title prefix). Run for real only
  AFTER the user stories have settled (see docs/WORK_TRACKING.md).

  It parses:
    - epics    from  '# EPIC <n> - <title>'         (en/em dash or colon ok)
    - personas from  the Personas legend table        ( | **TAG** | Name | ... )
    - stories  from  '**E<n>-<m>.** As a **TAG**, I want ... so that ...'
                     plus the following lines (Feature / AC) as the body.

  Epic milestones and epic:* / persona:* labels are created at run time from the
  file, never hardcoded, so they track however the stories are reorganized.

.PARAMETER StoriesFile
  Path to the user-stories markdown. Default: glean-user-stories.md
.PARAMETER Execute
  Omit (default) => dry run: print the plan and write nothing.
  Provide -Execute => create issues / milestones / labels and add to the board.
.PARAMETER ProjectTitle
  Projects board to add issues to. Default: "Beacon". Pass '' to skip.

.NOTES
  Requires the gh CLI authenticated with 'repo' and 'project' scopes.

.EXAMPLE
  powershell -File ./scripts/stories-to-issues.ps1
.EXAMPLE
  powershell -File ./scripts/stories-to-issues.ps1 -Execute
#>
[CmdletBinding()]
param(
  [string]$StoriesFile = "glean-user-stories.md",
  [switch]$Execute,
  [string]$ProjectTitle = "Beacon"
)

$ErrorActionPreference = "Stop"
$DryRun = -not $Execute

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  throw "gh CLI not found. Install GitHub CLI and run 'gh auth login' (scopes: repo, project)."
}
if (-not (Test-Path -LiteralPath $StoriesFile)) {
  throw "Stories file not found: $StoriesFile"
}

# --- repo + (optional) project handles ---
$repo = gh repo view --json nameWithOwner | ConvertFrom-Json
$nwo = $repo.nameWithOwner
Write-Host "Repo:  $nwo" -ForegroundColor Cyan

$projectNumber = $null
if ($ProjectTitle) {
  $list = gh project list --owner "@me" --format json | ConvertFrom-Json
  $proj = $list.projects | Where-Object { $_.title -eq $ProjectTitle } | Select-Object -First 1
  if ($proj) {
    $projectNumber = $proj.number
    Write-Host "Board: #$projectNumber  $($proj.url)" -ForegroundColor Cyan
  } else {
    Write-Warning "Board '$ProjectTitle' not found; run setup-board-and-labels.ps1 first. Skipping board adds."
  }
}

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$resolved = (Resolve-Path -LiteralPath $StoriesFile).ProviderPath
$lines = [System.IO.File]::ReadAllLines($resolved, [System.Text.Encoding]::UTF8)

# --- 1. personas legend:  | **KW** | Knowledge Worker | ... | ---
$personas = @{}
foreach ($line in $lines) {
  if ($line -match '^\|\s*\*\*(?<tag>[A-Z][A-Z0-9]+)\*\*\s*\|\s*(?<name>[^|]+?)\s*\|') {
    $personas[$matches.tag] = $matches.name.Trim()
  }
}
Write-Host "Personas: $(($personas.Keys | Sort-Object) -join ', ')"

# --- 2. walk the file, collecting stories ---
$stories = New-Object System.Collections.Generic.List[object]
$curEpicNum = $null
$curEpicTitle = $null
$cur = $null

function Add-CurrentStory {
  param($story)
  if ($null -ne $story) { $script:stories.Add($story) }
}

foreach ($line in $lines) {

  # epic heading: '# EPIC 1 - Enterprise Search (Glean Search)'
  if ($line -match '^#\s+EPIC\s+(?<n>\d+)\s*[-–—:]+\s*(?<t>.+?)\s*$') {
    $curEpicNum = [int]$matches.n
    $curEpicTitle = ($matches.t -replace '\s*\(.*\)\s*$', '').Trim()
    continue
  }

  # story header: '**E1-1.** As a **KW**, I want ...'
  if ($line -match '^\*\*(?<id>E\d+-\d+)\.\*\*\s*(?<rest>.*)$') {
    Add-CurrentStory $cur
    $storyId = $matches.id   # capture now: later -match calls clobber $matches
    $rest = $matches.rest

    # personas = bold tags on the first line that are known persona tags
    $ptags = @()
    foreach ($m in [regex]::Matches($rest, '\*\*(?<t>[A-Z][A-Za-z0-9-]+)\*\*')) {
      $t = $m.Groups['t'].Value
      if ($personas.ContainsKey($t) -and ($ptags -notcontains $t)) { $ptags += $t }
    }

    # capability for the title: the 'I want [to] ...' clause, up to ', so that'
    $cap = ($rest -replace '\*\*', '')
    if ($cap -match '(?i)I want(?:\s+to)?\s+(.+)$') { $cap = $matches[1] }
    $cap = (($cap -split '(?i),\s*so that')[0]).Trim().TrimEnd('.')
    if ([string]::IsNullOrWhiteSpace($cap)) { $cap = ($rest -replace '\*\*', '').Trim() }
    if ($cap.Length -gt 60) { $cap = $cap.Substring(0, 57).TrimEnd() + '...' }

    $cur = [pscustomobject]@{
      Id        = $storyId
      EpicNum   = $curEpicNum
      EpicTitle = $curEpicTitle
      Personas  = $ptags
      Title     = "[$storyId] $cap"
      FirstLine = ($rest -replace '\*\*', '').Trim()
      BodyLines = New-Object System.Collections.Generic.List[string]
    }
    continue
  }

  # any other heading ends the current story
  if ($line -match '^#') { Add-CurrentStory $cur; $cur = $null; continue }

  # otherwise accumulate body lines
  if ($null -ne $cur) { $cur.BodyLines.Add($line) }
}
Add-CurrentStory $cur

Write-Host "Parsed $($stories.Count) stories." -ForegroundColor Cyan
if ($stories.Count -eq 0) { throw "No stories parsed - check the file format against the regexes in this script." }

# --- 3. existing issues (idempotency by exact title) ---
$existingTitles = @{}
$existing = gh issue list --repo $nwo --state all --limit 1000 --json number,title | ConvertFrom-Json
foreach ($i in $existing) { $existingTitles[$i.title] = $i.number }

# --- helpers (used only on real runs) ---
$milestoneCache = @{}
function Resolve-Milestone {
  param([string]$title)
  if ($milestoneCache.ContainsKey($title)) { return $milestoneCache[$title] }
  $ms = gh api "repos/$nwo/milestones?state=all&per_page=100" | ConvertFrom-Json
  $hit = $ms | Where-Object { $_.title -eq $title } | Select-Object -First 1
  if (-not $hit) { $hit = gh api -X POST "repos/$nwo/milestones" -f title="$title" | ConvertFrom-Json }
  $milestoneCache[$title] = $hit.number
  return $hit.number
}
$ensuredLabels = @{}
function Confirm-Label {
  param([string]$name, [string]$color, [string]$desc)
  if ($ensuredLabels.ContainsKey($name)) { return }
  gh label create $name --color $color --description $desc --force | Out-Null
  $ensuredLabels[$name] = $true
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# --- 4. plan / create ---
$plan = New-Object System.Collections.Generic.List[object]
foreach ($s in $stories) {
  $labels = @("type:user-story", "epic:$($s.EpicNum)")
  foreach ($p in $s.Personas) { $labels += "persona:$($p.ToLower())" }
  $milestone = "EPIC $($s.EpicNum)"
  if ($s.EpicTitle) { $milestone += " - $($s.EpicTitle)" }
  $exists = $existingTitles.ContainsKey($s.Title)

  $plan.Add([pscustomobject]@{
      Id        = $s.Id
      Exists    = $exists
      Milestone = $milestone
      Labels    = ($labels -join ',')
      Title     = $s.Title
    })

  if ($DryRun -or $exists) { continue }

  # body
  $body = New-Object System.Collections.Generic.List[string]
  $body.Add("> Story ``$($s.Id)`` - generated from ``$StoriesFile``.")
  $body.Add("")
  $body.Add($s.FirstLine)
  $body.Add("")
  foreach ($bl in $s.BodyLines) { if ($bl.Trim()) { $body.Add($bl) } }
  $body.Add("")
  $body.Add("---")
  $body.Add("- **Epic:** $milestone")
  if ($s.Personas) { $body.Add("- **Persona(s):** $($s.Personas -join ', ')") }
  $body.Add("")
  $body.Add("<!-- beacon-story-id: $($s.Id) -->")
  $bodyText = ($body -join "`n")

  # ensure labels + milestone exist
  Confirm-Label "epic:$($s.EpicNum)" "5319e7" "Stories under EPIC $($s.EpicNum)"
  foreach ($p in $s.Personas) { Confirm-Label "persona:$($p.ToLower())" "c2e0c6" "Persona: $($personas[$p])" }
  Resolve-Milestone $milestone | Out-Null

  $tmp = New-TemporaryFile
  [System.IO.File]::WriteAllText($tmp.FullName, $bodyText, $utf8NoBom)
  $ghArgs = @("issue", "create", "--repo", $nwo, "--title", $s.Title, "--body-file", $tmp.FullName, "--milestone", $milestone)
  foreach ($l in $labels) { $ghArgs += @("--label", $l) }
  $url = gh @ghArgs
  Remove-Item $tmp.FullName -Force
  Write-Host "  created $($s.Id): $url" -ForegroundColor Green

  if ($projectNumber) { gh project item-add $projectNumber --owner "@me" --url $url | Out-Null }
}

# --- 5. report ---
$toCreate = ($plan | Where-Object { -not $_.Exists }).Count
$skip = ($plan | Where-Object { $_.Exists }).Count
Write-Host ""
$plan | Format-Table Id, Exists, Milestone, Title -AutoSize
Write-Host ""
if ($DryRun) {
  Write-Host "DRY RUN - nothing created. $toCreate would be created; $skip already exist." -ForegroundColor Yellow
  Write-Host "Re-run with -Execute to create them." -ForegroundColor Yellow
} else {
  Write-Host "Done. Created $toCreate; skipped $skip existing." -ForegroundColor Green
}

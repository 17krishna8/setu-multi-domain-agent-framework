<#
.SYNOPSIS
  Setu installer for Windows PowerShell. Mirrors install.sh.

.DESCRIPTION
  .\install.ps1                                  # claude, all packs
  .\install.ps1 -Target cursor                    # .cursor\rules\
  .\install.ps1 -Target codex                     # AGENTS.md
  .\install.ps1 -Profile minimal -Domains cyber,finance
  .\install.ps1 -DryRun
  .\install.ps1 -Uninstall -Target claude         # removes exactly what it wrote
  .\install.ps1 -List

  Uninstall is ledger-driven: every file written is recorded in
  ~\.setu\install-ledger.json, and uninstall deletes those paths only. Files we
  merely EDITED (AGENTS.md) are tracked separately — uninstall strips the
  Setu-managed block between the markers and leaves everything else alone, so a
  user's own notes are never destroyed.
#>
[CmdletBinding()]
param(
  [ValidateSet('claude','cursor','codex')][string]$Target = 'claude',
  [ValidateSet('full','minimal')][string]$Profile = 'full',
  [string]$Domains = '',
  [switch]$Uninstall,
  [switch]$List,
  [switch]$DryRun,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$Ledger = if ($env:SETU_LEDGER) { $env:SETU_LEDGER } else { Join-Path $HOME '.setu\install-ledger.json' }
$env:SETU_LEDGER = $Ledger

$AllDomains = @('cyber','webdev','admin','finance','agri','education')
$MinimalDomains = @('cyber','finance')

$SharedScripts = @('memory.py','model_wrapper.py','common.py','install_state.py','setu.py')
$PackShared = @{
  'pattern_engine.py' = @('cyber','finance')
  'severity_scorer.py' = @('cyber')
  'dedup.py' = @('cyber')
}
$DomainScripts = @{
  cyber   = @('cyber_recon.py','cyber_scan.py','cyber_chain.py','report_formatter.py')
  webdev  = @('design_check.py','webdev_fix.py')
  admin   = @('admin_docs.py')
  finance = @('finance.py')
  agri    = @('agri.py', 'agri_image.py', 'agri_model.py')
  education = @('education.py')
}
$DomainRules = @{
  cyber   = @('patterns.json','dependencies.json')
  webdev  = @()
  admin   = @('legal_corpus.json')
  finance = @('patterns.json')
  agri    = @()
  education = @()
}

function Say($msg) { Write-Host $msg }
function Invoke-Step { param([scriptblock]$Action, [string]$Description)
  if ($DryRun) { Say "  would: $Description" } else { & $Action }
}

# ---------------------------------------------------------------- preflight
try { & $Py --version | Out-Null } catch {
  throw "Python not found. Install Python 3.9+ and ensure '$Py' is on PATH (or set `$env:PYTHON)."
}

if ($List) { & $Py (Join-Path $ScriptDir 'scripts\install_state.py') list; exit $LASTEXITCODE }

if ($Uninstall) {
  $uArgs = @((Join-Path $ScriptDir 'scripts\install_state.py'), 'uninstall', '--target', $Target)
  if ($DryRun) { $uArgs += '--dry-run' }
  & $Py @uArgs
  exit $LASTEXITCODE
}

# ---------------------------------------------------------------- domains
if ([string]::IsNullOrWhiteSpace($Domains)) {
  $Domains = if ($Profile -eq 'minimal') { $MinimalDomains -join ',' } else { $AllDomains -join ',' }
}
$Selected = $Domains.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
foreach ($d in $Selected) {
  if ($AllDomains -notcontains $d) {
    throw "Unknown domain '$d'. Available: $($AllDomains -join ', ')"
  }
}

# ---------------------------------------------------------------- root
$Root = switch ($Target) {
  'claude' { if ($env:CLAUDE_HOME) { $env:CLAUDE_HOME } else { Join-Path $HOME '.claude' } }
  'cursor' { if ($env:CURSOR_HOME) { $env:CURSOR_HOME } else { Join-Path (Get-Location) '.cursor' } }
  'codex'  { if ($env:CODEX_HOME)  { $env:CODEX_HOME }  else { (Get-Location).Path } }
}
if ($DryRun) {
  $Root = [System.IO.Path]::GetFullPath($Root)
} else {
  if (-not (Test-Path $Root)) { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
  $Root = (Resolve-Path $Root).Path
}

$Manifest = New-TemporaryFile
$Shared   = New-TemporaryFile

Say 'Setu installer'
Say "  target   : $Target  ($Root)"
Say "  profile  : $Profile  (domains: $Domains)"
if ($DryRun) { Say '  DRY RUN - nothing will be written' }
Say ''

function Copy-SetuFile {
  param([string]$Src, [string]$Dest)
  $dir = Split-Path -Parent $Dest
  if (-not $DryRun -and -not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
  }
  if ((Test-Path $Dest) -and (-not $Force)) {
    $same = (Get-FileHash $Src).Hash -eq (Get-FileHash $Dest).Hash
    if ($same) {
      Say "  = $([System.IO.Path]::GetFileName($Dest)) (unchanged)"
      Add-Content -Path $Manifest -Value $Dest
      return
    }
    Say "  ! $([System.IO.Path]::GetFileName($Dest)) exists and differs; backing up"
    if (-not $DryRun) { Copy-Item $Dest "$Dest.setu-bak" -Force }
    Add-Content -Path $Manifest -Value "$Dest.setu-bak"
  }
  if (-not $DryRun) { Copy-Item $Src $Dest -Force }
  Add-Content -Path $Manifest -Value $Dest
  Say "  + $($Dest.Replace($Root, '').TrimStart('\'))"
}

function Resolve-PackShared {
  param([string]$Domain)
  $out = @()
  foreach ($script in $PackShared.Keys) {
    if ($PackShared[$script] -contains $Domain) { $out += $script }
  }
  return ($out | Sort-Object -Unique)
}

# ---------------------------------------------------------------- install
Say 'Shared:'
foreach ($f in $SharedScripts) {
  Copy-SetuFile (Join-Path $ScriptDir "scripts\$f") (Join-Path $Root "setu\scripts\$f")
}
if ($Target -eq 'claude') {
  Copy-SetuFile (Join-Path $ScriptDir 'skills\setu-orchestrator\SKILL.md') `
                (Join-Path $Root 'skills\setu-orchestrator\SKILL.md')
}
Say ''

foreach ($d in $Selected) {
  Say "Domain: $d"
  foreach ($f in (Resolve-PackShared $d)) {
    Copy-SetuFile (Join-Path $ScriptDir "scripts\$f") (Join-Path $Root "setu\scripts\$f")
  }
  foreach ($f in $DomainScripts[$d]) {
    Copy-SetuFile (Join-Path $ScriptDir "scripts\$f") (Join-Path $Root "setu\scripts\$f")
  }
  foreach ($r in $DomainRules[$d]) {
    $ruleDest = if ($Target -eq 'claude') { "setu\scripts\rules\$r" } else { "setu\rules\$r" }
    Copy-SetuFile (Join-Path $ScriptDir "scripts\rules\$r") (Join-Path $Root $ruleDest)
  }
  Get-ChildItem (Join-Path $ScriptDir 'agents') -Filter "$d-*.md" | Sort-Object Name | ForEach-Object {
    switch ($Target) {
      'claude' { Copy-SetuFile $_.FullName (Join-Path $Root "agents\$($_.Name)") }
      'cursor' { Copy-SetuFile $_.FullName (Join-Path $Root "rules\$($_.BaseName).mdc") }
      'codex'  { Copy-SetuFile $_.FullName (Join-Path $Root "setu\agents\$($_.Name)") }
    }
  }
  $skillSrc = Join-Path $ScriptDir "skills\setu-$d\SKILL.md"
  switch ($Target) {
    'claude' { Copy-SetuFile $skillSrc (Join-Path $Root "skills\setu-$d\SKILL.md") }
    'cursor' { Copy-SetuFile $skillSrc (Join-Path $Root "rules\setu-$d.mdc") }
    'codex'  { Copy-SetuFile $skillSrc (Join-Path $Root "setu\skills\setu-$d\SKILL.md") }
  }
  Say ''
}

# ---------------------------------------------------------------- codex AGENTS.md
if ($Target -eq 'codex') {
  Say 'Codex:'
  $agentsMd = Join-Path $Root 'AGENTS.md'
  # Built as a LITERAL here-string (@'...'@) on purpose. In a double-quoted
  # here-string, backtick is PowerShell's escape character, so Markdown fences
  # written as `` collapse to a single backtick and the block renders wrong.
  # Literal + explicit placeholders keeps the escaping unambiguous.
  $blockTemplate = @'
<!-- BEGIN SETU (managed block - do not edit inside) -->
# Setu - multi-domain agent framework

This block is managed by the Setu installer. Remove it by running
`install.ps1 -Uninstall -Target codex`; anything outside the markers is left
untouched.

Installed packs: **__DOMAINS__** (profile: __PROFILE__)

| Request is about... | Pack | Read |
|---|---|---|
| security testing, recon, bug bounty | cyber | `skills/setu-cyber/SKILL.md` |
| building a site/component, fixing code | webdev | `skills/setu-webdev/SKILL.md` |
| RTI, grievance, consumer complaint | admin | `skills/setu-admin/SKILL.md` |
| expenses, scams, EMI/loans | finance | `skills/setu-finance/SKILL.md` |
| crop disease, pests, treatment | agri | `skills/setu-agri/SKILL.md` |
| study planning, revision, practice | education | `skills/setu-education/SKILL.md` |

Non-negotiable gates:
- cyber: no active testing without confirmed authorization.
- webdev: no fix accepted without a green test run.
- admin: never final without a named human confirmation.
- finance: never a model-generated number; EMI maths is Python only.
- agri: no treatment advice below 0.6 confidence.
- education: coach learning; do not complete graded work for the learner.

Helper scripts:

```powershell
python setu\scripts\setu.py doctor
python setu\scripts\setu.py selftest
```
<!-- END SETU -->
'@
  $block = $blockTemplate.Replace('__DOMAINS__', $Domains).Replace('__PROFILE__', $Profile)

  if (-not (Test-Path $agentsMd)) {
    Say '  creating AGENTS.md'
    if (-not $DryRun) { Set-Content -Path $agentsMd -Value $block -Encoding UTF8 }
    Add-Content -Path $Manifest -Value $agentsMd
  } else {
    $text = Get-Content $agentsMd -Raw
    $begin = '<!-- BEGIN SETU (managed block'
    $endMarker = '<!-- END SETU -->'
    if ($text -like "*$begin*") {
      Say '  updating the existing Setu block (content outside it preserved)'
      if (-not $DryRun) {
        Copy-Item $agentsMd "$agentsMd.setu-bak" -Force
        $start = $text.IndexOf($begin)
        $stop = $text.IndexOf($endMarker, $start) + $endMarker.Length
        $new = $text.Substring(0, $start) + $block + $text.Substring($stop)
        Set-Content -Path $agentsMd -Value $new -Encoding UTF8
      }
      Add-Content -Path $Shared -Value $agentsMd
    } else {
      Say '  appending a Setu block to the existing AGENTS.md (your content is untouched)'
      if (-not $DryRun) {
        Copy-Item $agentsMd "$agentsMd.setu-bak" -Force
        Add-Content -Path $agentsMd -Value "`r`n`r`n$block"
      }
      Add-Content -Path $Shared -Value $agentsMd
    }
  }
  Say ''
}

# ---------------------------------------------------------------- record
if (-not $DryRun) {
  $sharedArgs = @()
  if ((Get-Item $Shared).Length -gt 0) {
    Get-Content $Shared | Where-Object { $_ } | ForEach-Object { $sharedArgs += @('--shared', $_) }
  }
  & $Py (Join-Path $ScriptDir 'scripts\install_state.py') record `
      --target $Target --root $Root --domains $Domains --manifest $Manifest @sharedArgs | Out-Null
  $count = (Get-Content $Manifest | Sort-Object -Unique | Measure-Object).Count
  Say "Installed $count unique file(s)."
  Say "Ledger: $Ledger (uninstall reads this)"
} else {
  Say 'Dry run complete - nothing written.'
}

Remove-Item $Manifest, $Shared -Force -ErrorAction SilentlyContinue
Say ''
Say 'Next:'
switch ($Target) {
  'claude' { Say '  Restart Claude Code, then: "setu: audit the security of <in-scope target>"' }
  'cursor' { Say '  Cursor picks up .cursor\rules automatically. Ask: "using the setu rules, ..."' }
  'codex'  { Say '  Codex reads AGENTS.md. Ask: "using setu, draft an RTI application for ..."' }
}
Say "  Remove:  .\install.ps1 -Uninstall -Target $Target"

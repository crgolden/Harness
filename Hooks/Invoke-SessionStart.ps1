<#
.SYNOPSIS
SessionStart hook: reports AGENT_MACHINE_TYPE, puts the Stop-gate rules card in context and, on Windows Server 2019, keeps the Claude Code console usable.

.DESCRIPTION
AGENT_MACHINE_TYPE is read from the environment, or derived from the OS caption and persisted as a User variable.

The Stop-gate rules card (stop-gate-rules.txt beside this script) is added to the session's context in full. A Stop
block names only the check that failed; the card holds the rules behind every block, so it must be in context on
every source SessionStart fires for: startup, resume, clear and compact. A missing card is reported to the operator
and the agent rather than skipped. The artifact-usage skill's rules (its SKILL.md without the front matter) are added
the same way, on every source, and a missing skill is reported the same way.

Windows Server 2019 is detected from the live registry (InstallationType Server, build 17763), never from
AGENT_MACHINE_TYPE, so a stale or copied value cannot switch a Windows 11 machine. Every other machine has the
User variable below removed if it is ever present.

On Windows Server 2019 the fullscreen renderer leaves the conversation unscrollable on legacy conhost, and
~/.claude/settings.json (which sets "tui": "fullscreen") is synced to every machine. The per-machine override is
the User variable CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1, which Claude Code checks before the tui setting. The
renderer is chosen at launch, so a session started without it is told to restart.

The same machine loses drag-select and right-click paste once Claude Code enters raw mode, so
Restore-ConsoleQuickEdit.ps1 is started detached, after a delay that lets raw mode happen first.

Every skill under Tools/Claude/skills gets its junction in the workspace's project skills folder,
<workspace>\.claude\skills, since junctions do not sync and a skill added on one machine is otherwise missing on the
rest (SKILLS-AS-LINKS.md). Never ~/.claude/skills: OneDrive writes the files behind a junction there into every
other machine's Tools checkout, and a junction or copy removed there deletes the other machines' skill folders. A
missing entry is created. An existing junction is left as it is and reported when it points elsewhere. A real
directory is handed to Deploy-ClaudeSkills.ps1 -Migrate, run in this process: a copy whose every file the repo already
holds becomes a junction, and a copy holding anything else is left untouched and reported to the operator, because
removing it could delete the only copy of a file. Whatever ~/.claude/skills still holds is reported in the context and
never touched; Claude/Retire-UserSkillLinks.ps1 retires it across the fleet. Nothing here can stop the session from
starting: a failure is reported in the context.

Every session's scratchpad and background task output, and the hooks' state beside them, live under the account's
own %TEMP%\claude (claude_temp.py), which no other account reads. When CLAUDE_CODE_TMPDIR names a machine-wide root
instead, the hook creates it when missing and holds its ACL to the running account, Administrators and SYSTEM with
inheritance cut, so no other account on the machine (the triage agent's included) can read a session's files. An ACL
already in that shape is left alone; any other is replaced and reported. No root set means no folder to secure.

Work-product declarations and parking lots live under %TEMP%\claude (session_home.py), and nothing reads them anywhere
else.

Before anything is read from it, the Tools checkout this hook runs from is fast-forwarded to origin's default branch by
the fleet audit's own Sync-CrgoldenRepo (Claude/skills/fleet-audit/scripts/RepoSync.ps1): only on its default branch,
with no local commits, no modified file that differs from origin, and no gate holding the account's slot. A checkout
left behind is reported to the operator and to the session, since a session on a stale checkout reads hooks, skills
and config older than origin's and takes a file the checkout lacks for proof that what it names does not exist. The
coordination board's URL is then read from coordination.json and put in context, and a missing file is reported as
a stale checkout, never as no board.

A line handing the operator a decision or an action opens with "OPERATOR ACTION:". The Stop gate refuses a DONE:
while a line opening with it is quoted by no question on the session's parking-lot page, so the marker, not the
wording, is what makes a line one the session must park.

A session whose working directory is the Tools checkout this hook runs from gets a systemMessage telling the operator
to start Claude Code in the crgolden workspace: every session runs there, and a session in Tools keeps a separate
project folder no other session or hook reads.

A session whose working directory is outside the workspace (another tool launching Claude Code, such as an editor
extension's install folder) gets one short line and none of the session context. Claude Code saves a hook output too
large to show inline into ~/.claude/projects/<that directory>, and OneDrive carries that stray folder to every machine.

After a compaction (source compact) the operator's answers this session read back from its own parking-lot page are
put back in context verbatim (answered_questions.py), since the summary paraphrases or drops them and a session that
lost one asks it again. A failure to read them is reported in the context, never fatal.
#>
[CmdletBinding()]
param(
    [int] $QuickEditDelaySeconds = 8,
    [string] $WindowsVersionKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion',
    [string] $RulesCardPath = (Join-Path $PSScriptRoot 'stop-gate-rules.txt'),
    [string] $SkillsCheckout = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'Tools'),
    [string] $ArtifactSkillPath = (Join-Path $SkillsCheckout 'Claude\skills\artifact-usage\SKILL.md'),
    [string] $SkillSource = (Join-Path $SkillsCheckout 'Claude\skills'),
    [string] $SkillDestination = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) '.claude\skills'),
    [string] $LegacySkillRoot = (Join-Path $env:USERPROFILE '.claude\skills'),
    [string] $SkillDeployScript = (Join-Path $SkillsCheckout 'Claude\Deploy-ClaudeSkills.ps1'),
    [string] $ClaudeTempRoot,
    [string] $ToolsCheckout = (Split-Path -Parent $PSScriptRoot),
    [string] $CoordinationPath = (Join-Path $PSScriptRoot 'coordination.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OperatorActionMarker = 'OPERATOR ACTION:'
. (Join-Path $PSScriptRoot 'ClaudeTemp.ps1')
if (-not $PSBoundParameters.ContainsKey('ClaudeTempRoot')) { $ClaudeTempRoot = Get-ClaudeTempOverride }

function Get-WantedTempRootSids {
    @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
        'S-1-5-32-544',
        'S-1-5-18'
    )
}

function Test-TempRootAcl {
    param([System.Security.AccessControl.DirectorySecurity] $Acl, [string[]] $WantedSids)
    if (-not $Acl.AreAccessRulesProtected) { return $false }
    $rules = @($Acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne $WantedSids.Count) { return $false }
    foreach ($rule in $rules) {
        $full = $rule.FileSystemRights -eq [System.Security.AccessControl.FileSystemRights]::FullControl
        $inherits = $rule.InheritanceFlags -eq ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit)
        $allows = $rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow
        if (-not ($full -and $inherits -and $allows) -or $WantedSids -notcontains $rule.IdentityReference.Value) { return $false }
    }
    return $true
}

function Protect-ClaudeTempRoot {
    param([string] $Path)
    $created = $false
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
        $created = $true
    }
    $wanted = Get-WantedTempRootSids
    $acl = Get-Acl -LiteralPath $Path
    if (Test-TempRootAcl -Acl $acl -WantedSids $wanted) {
        return "Claude temp root $Path is secured to this account, Administrators and SYSTEM."
    }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))) {
        $acl.RemoveAccessRule($rule) | Out-Null
    }
    foreach ($sid in $wanted) {
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($sid),
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    $verb = if ($created) { 'Created' } else { 'Re-secured' }
    return "$verb the Claude temp root $Path and cut its inherited ACL: only this account, Administrators and SYSTEM can read it."
}

$machineType = $env:AGENT_MACHINE_TYPE
if (-not $machineType) {
    $machineType = (Get-CimInstance Win32_OperatingSystem).Caption
    [Environment]::SetEnvironmentVariable('AGENT_MACHINE_TYPE', $machineType, 'User')
}

$context = @("AGENT_MACHINE_TYPE=$machineType")
$systemMessages = @()
$rendererVariable = 'CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'
$staleTail = 'Hooks, skills and coordination.json in this session may be older than origin''s; a file this checkout lacks is a stale checkout, never proof that what it names does not exist.'

if ($ToolsCheckout) {
    $savedToken = $env:GH_TOKEN
    try {
        . (Join-Path $PSScriptRoot '..\Gates\GateSlot.ps1')
        . (Join-Path $SkillsCheckout 'Claude\skills\fleet-audit\scripts\RepoSync.ps1')
        if ($env:GITHUB_PERSONAL_ACCESS_TOKEN) { $env:GH_TOKEN = $env:GITHUB_PERSONAL_ACCESS_TOKEN }
        $env:GIT_TERMINAL_PROMPT = '0'
        $holder = Get-GateSlotHolder
        $sync = Sync-CrgoldenRepo $ToolsCheckout $(if ($holder) { [string] $holder.owner } else { '' })
        if ($sync.State -eq 'current') {
            $context += "Tools at $ToolsCheckout is current with origin/$($sync.Default) ($($sync.From))."
        }
        elseif ($sync.State -eq 'advanced') {
            $context += "Fast-forwarded Tools at $ToolsCheckout from $($sync.From) to $($sync.To) (origin/$($sync.Default)) before reading the rules card, the skills and coordination.json$(if ($sync.Detail) { "; $($sync.Detail)" })."
        }
        else {
            $note = "Tools at $ToolsCheckout was not brought to origin ($($sync.State)$(if ($sync.Detail) { ": $($sync.Detail)" }))"
            $systemMessages += "$note."
            $context += "$OperatorActionMarker $note. $staleTail"
        }
    }
    catch {
        $systemMessages += "Tools at $ToolsCheckout was not checked against origin: $($_.Exception.Message)"
        $context += "WARNING: Tools at $ToolsCheckout was not checked against origin ($($_.Exception.Message)). $staleTail"
    }
    finally {
        if ($null -eq $savedToken) { Remove-Item -Path 'env:GH_TOKEN' -ErrorAction SilentlyContinue } else { $env:GH_TOKEN = $savedToken }
    }
}

if (Test-Path -LiteralPath $CoordinationPath -PathType Leaf) {
    try {
        $board = [string] (Get-Content -LiteralPath $CoordinationPath -Raw | ConvertFrom-Json).boardUrl
        if ([string]::IsNullOrWhiteSpace($board)) { throw 'it names no boardUrl' }
        $context += "Coordination board: $board (Hooks/coordination.json). Register this session on it before any goal (the coordination skill); every cross-session and cross-machine row goes there."
    }
    catch {
        $context += "WARNING: $CoordinationPath could not be read ($($_.Exception.Message)), so the coordination board's URL is unknown here; it is not evidence that no board exists. Ask the operator for the URL."
    }
}
else {
    $context += "WARNING: $CoordinationPath is missing, so this Tools checkout is older than the coordination board, not proof that no board exists. $staleTail"
}

if (Test-Path -LiteralPath $RulesCardPath) {
    $context += "Stop-gate rules, in force for every turn of this session (Harness/Hooks/stop-gate-rules.txt):`n" +
        (Get-Content -LiteralPath $RulesCardPath -Raw)
}
else {
    $systemMessages += "The Stop-gate rules card is missing at $RulesCardPath, so this session started without it. Redeploy the hooks."
    $context += 'WARNING: the Stop-gate rules card did not load. Read Harness/Hooks/stop-gate-rules.txt in full before ending any turn.'
}

if (Test-Path -LiteralPath $ArtifactSkillPath) {
    $skillBody = (Get-Content -LiteralPath $ArtifactSkillPath -Raw) -replace '(?s)\A---\r?\n.*?\r?\n---\r?\n', ''
    $context += "The artifact-usage skill, in force for every write to a parking-lot page (Tools/Claude/skills/artifact-usage/SKILL.md):`n" +
        $skillBody
}
else {
    $systemMessages += "The artifact-usage skill is missing at $ArtifactSkillPath, so this session started without it. Pull the Tools checkout on main."
    $context += 'WARNING: the artifact-usage skill did not load. Read Tools/Claude/skills/artifact-usage/SKILL.md in full before writing to any parking-lot page.'
}

try {
    if (-not (Test-Path -LiteralPath $SkillDestination)) { New-Item -ItemType Directory -Path $SkillDestination -Force | Out-Null }
    $copies = @()
    foreach ($skill in @(Get-ChildItem -LiteralPath $SkillSource -Directory -ErrorAction Stop)) {
        $link = Join-Path $SkillDestination $skill.Name
        $wanted = $skill.FullName.TrimEnd('\')
        if (-not (Test-Path -LiteralPath $link)) {
            New-Item -ItemType Junction -Path $link -Target $wanted | Out-Null
            $context += "Linked the new skill $($skill.Name): $link -> $wanted"
            continue
        }
        $existing = Get-Item -LiteralPath $link -Force
        if (-not $existing.LinkType) {
            $copies += $skill.Name
        }
        elseif ((@($existing.Target)[0]).TrimEnd('\') -ine $wanted) {
            $context += "Skill $($skill.Name) at $link links to $(@($existing.Target)[0]), not $wanted. Left untouched."
        }
    }
    if ($copies) {
        $report = (@(& $SkillDeployScript -Source $SkillSource -Destination $SkillDestination -SkillName $copies -Migrate) -join "`n").Trim()
        if ($report -match '\bBLOCKED\b') {
            $systemMessages += "Skill copies under $SkillDestination are real directories, not links, and at least one holds a file the repo lacks, so it was left untouched:`n$report"
        }
        $context += "Skill copies found as real directories, handed to Deploy-ClaudeSkills.ps1 -Migrate:`n$report"
    }
}
catch {
    $context += "Skill links were not checked: $($_.Exception.Message)"
}

try {
    $legacy = @(Get-ChildItem -LiteralPath $LegacySkillRoot -Force -ErrorAction Stop | Where-Object { $_.Name -ne 'synced' } | ForEach-Object { $_.Name })
    if ($legacy.Count) {
        $context += "~\.claude\skills still holds $($legacy -join ', '), left untouched: removing any of them deletes the same skill from the other machines' Tools checkouts through OneDrive, so they go only with Claude/Retire-UserSkillLinks.ps1 (SKILLS-AS-LINKS.md)."
    }
}
catch {
    $legacy = @()
}

if ($ClaudeTempRoot) {
    try {
        $context += Protect-ClaudeTempRoot -Path $ClaudeTempRoot
    }
    catch {
        $systemMessages += "The Claude temp root $ClaudeTempRoot was not secured: $($_.Exception.Message)"
        $context += "WARNING: the Claude temp root $ClaudeTempRoot was not secured ($($_.Exception.Message)); other accounts on this machine may read this session's scratch and task output."
    }
}
else {
    $context += "CLAUDE_CODE_TMPDIR is not set: the session scratchpad, task output and the hooks' state are under $(Get-ClaudeTempRoot), the account's own temp folder; the read-scope gate lets a session read only its own files there."
}

$windowsVersion = Get-ItemProperty -LiteralPath $WindowsVersionKey
$isServer2019 = $windowsVersion.InstallationType -eq 'Server' -and $windowsVersion.CurrentBuild -eq '17763'

if (-not $isServer2019) {
    if ([Environment]::GetEnvironmentVariable($rendererVariable, 'User')) {
        [Environment]::SetEnvironmentVariable($rendererVariable, [NullString]::Value, 'User')
        $context += "Removed the User variable ${rendererVariable}: it belongs only on Windows Server 2019."
    }
}
else {
    if ([Environment]::GetEnvironmentVariable($rendererVariable, 'User') -ne '1') {
        [Environment]::SetEnvironmentVariable($rendererVariable, '1', 'User')
    }

    if ([Environment]::GetEnvironmentVariable($rendererVariable, 'Process') -eq '1') {
        $context += "$rendererVariable=1 (classic renderer, native scrollback)"
    }
    else {
        $systemMessages += "This session started in fullscreen mode, so scrollback is unreachable on Windows Server 2019. " +
            "$rendererVariable=1 is now set for this user; type /exit and start Claude Code from a NEW PowerShell window."
        $context += "$OperatorActionMarker $rendererVariable is not set in this session: the operator must restart Claude Code from a new window to get scrollback."
    }

    Start-Process -FilePath 'pwsh' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-File', (Join-Path $PSScriptRoot 'Restore-ConsoleQuickEdit.ps1'),
        '-DelaySeconds', $QuickEditDelaySeconds
    ) | Out-Null
    $context += "Restore-ConsoleQuickEdit.ps1 scheduled in ${QuickEditDelaySeconds}s (drag-select and right-click paste)."
}

$hookInput = $null
try {
    $hookInput = [Console]::In.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop
}
catch {
    $hookInput = $null
}
$sessionCwd = if ($hookInput -and $hookInput.PSObject.Properties['cwd']) { [string] $hookInput.cwd } else { '' }
$toolsCwd = (Split-Path -Parent $PSScriptRoot).TrimEnd('\')
if ($sessionCwd -and [string]::Equals([IO.Path]::GetFullPath($sessionCwd).TrimEnd('\'), $toolsCwd, [StringComparison]::OrdinalIgnoreCase)) {
    $systemMessages += "This session started in the Tools checkout ($sessionCwd). Start Claude Code in the crgolden workspace instead: a session in Tools keeps a separate project folder, transcripts and memory link that the rest of the fleet does not read."
    $context += "WARNING: this session's working directory is the Tools checkout, not the crgolden workspace; the operator was told to start sessions in the workspace."
}
$workspaceRoot = (Split-Path -Parent $toolsCwd).TrimEnd('\')
if ($sessionCwd) {
    $fullCwd = [IO.Path]::GetFullPath($sessionCwd).TrimEnd('\')
    $inWorkspace = [string]::Equals($fullCwd, $workspaceRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $fullCwd.StartsWith($workspaceRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    if (-not $inWorkspace) {
        $output = @{ hookSpecificOutput = @{ hookEventName = 'SessionStart'; additionalContext =
                    "This session started outside the crgolden workspace ($sessionCwd), so the crgolden session context (the Stop-gate rules card, the artifact-usage skill, the coordination board) was not loaded." } }
        if ($systemMessages) { $output.systemMessage = $systemMessages -join "`n" }
        $output | ConvertTo-Json -Compress
        return
    }
}
$compactTranscript = if ($hookInput -and $hookInput.PSObject.Properties['source'] -and $hookInput.source -eq 'compact' -and
    $hookInput.PSObject.Properties['transcript_path']) { [string] $hookInput.transcript_path } else { $null }
if ($compactTranscript) {
    try {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $answered = (@(& python (Join-Path $PSScriptRoot 'answered_questions.py') $compactTranscript) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "answered_questions.py exited $LASTEXITCODE" }
        if ($answered) { $context += $answered }
    }
    catch {
        $context += "WARNING: the operator's parking-lot answers were not restored after the compaction ($($_.Exception.Message)). Read the page's answered questions before asking anything."
    }
}

$output = @{ hookSpecificOutput = @{ hookEventName = 'SessionStart'; additionalContext = ($context -join "`n") } }
if ($systemMessages) {
    $output.systemMessage = $systemMessages -join "`n"
}
$output | ConvertTo-Json -Compress

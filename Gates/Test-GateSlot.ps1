# GATE-STEP: Gate slot harness
<#
.SYNOPSIS
    Harness for GateSlot.ps1 alone: one gate at a time per account. A held slot refuses a second holder and fails a
    gate naming the holder, the launching process's token lets its own child gate through and a wrong token does not, a
    holder record whose process is gone is taken over, and the slot lives in the account's own CLAUDE_CODE_TMPDIR when
    set, else %TEMP%\claude, never a machine-wide folder. Each case runs in its own pwsh with its own temp root, since
    a refused gate ends its process.
#>
. (Join-Path $PSScriptRoot '..\Hooks\ClaudeTemp.ps1')

$failures = 0
$module = Join-Path $PSScriptRoot 'GateSlot.ps1'
$work = New-ClaudeTempPath "gate-slot-test-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null

$cases = [ordered]@{
    'free'          = @{ Exit = 0; Wanted = @('SLOT first=True holder=probe first', 'SLOT second=True holder=probe second', 'SLOT after-exit holder=False'); Unwanted = @('GATE: FAILED'); Code = @'
$first = Enter-GateSlot -Owner 'probe first' -TimeoutMinutes 0
Write-Host "SLOT first=$([bool]$first) holder=$((Get-GateSlotHolder).owner)"
Exit-GateSlot -Slot $first
$second = Enter-GateSlot -Owner 'probe second' -TimeoutMinutes 0
Write-Host "SLOT second=$([bool]$second) holder=$((Get-GateSlotHolder).owner)"
Exit-GateSlot -Slot $second
Write-Host "SLOT after-exit holder=$([bool](Get-GateSlotHolder))"
'@ }
    'release-strict' = @{ Exit = 0; Wanted = @('SLOT released-strict holder=False'); Unwanted = @('Value cannot be null', 'GATE: FAILED'); Code = @'
$ErrorActionPreference = 'Stop'
$held = Enter-GateSlot -Owner 'probe strict' -TimeoutMinutes 0
Exit-GateSlot -Slot $held
Write-Host "SLOT released-strict holder=$([bool](Get-GateSlotHolder))"
'@ }
    'contended'     = @{ Exit = 1; Wanted = @('SLOT contender=False', 'gate slot is held by probe holder'); Unwanted = @('launching process'); Code = @'
$held = Enter-GateSlot -Owner 'probe holder' -TimeoutMinutes 0
Write-Host "SLOT contender=$([bool](Enter-GateSlot -Owner 'probe contender' -TimeoutMinutes 0))"
$null = Enter-GateSlotForGate 'probe'
Exit-GateSlot -Slot $held
'@ }
    'token'         = @{ Exit = 0; Wanted = @('gate slot held by the launching process: probe launcher'); Unwanted = @('GATE: FAILED'); Code = @'
$held = Enter-GateSlot -Owner 'probe launcher' -TimeoutMinutes 0
$env:CRGOLDEN_GATE_SLOT_TOKEN = $held.token
$null = Enter-GateSlotForGate 'probe'
Exit-GateSlot -Slot $held
'@ }
    'wrong-token'   = @{ Exit = 1; Wanted = @('gate slot is held by probe launcher'); Unwanted = @('launching process'); Code = @'
$held = Enter-GateSlot -Owner 'probe launcher' -TimeoutMinutes 0
$env:CRGOLDEN_GATE_SLOT_TOKEN = [Guid]::NewGuid().ToString('N')
$null = Enter-GateSlotForGate 'probe'
Exit-GateSlot -Slot $held
'@ }
    'unopenable'    = @{ Exit = 1; Wanted = @("GATE: FAILED (could not open this account's gate slot"); Unwanted = @('SLOT '); Code = @'
New-Item -ItemType Directory -Force -Path (Join-Path (Get-GateSlotDirectory) 'gate-slot.lock') | Out-Null
$null = Enter-GateSlotForGate 'probe'
'@ }
    'per-user'      = @{ Exit = 0; Wanted = @('SLOT default-is-account-private=True', 'SLOT follows-claude-code-tmpdir=True'); Unwanted = @('=False'); Code = @'
$env:CRGOLDEN_GATE_SLOT_DIRECTORY = Join-Path $Temp 'planted-override'
$expected = Join-Path (ConvertTo-LongPath $env:TEMP) 'claude\crgolden-gates'
$private = [string]::Equals((Get-GateSlotDirectory), $expected, [StringComparison]::OrdinalIgnoreCase)
Write-Host "SLOT default-is-account-private=$($private -and -not (Get-GateSlotDirectory).StartsWith($env:ProgramData, [StringComparison]::OrdinalIgnoreCase))"
$env:CLAUDE_CODE_TMPDIR = Join-Path $Temp 'tmpdir'
$followed = Join-Path (ConvertTo-LongPath $env:CLAUDE_CODE_TMPDIR) 'claude\crgolden-gates'
Write-Host "SLOT follows-claude-code-tmpdir=$([string]::Equals((Get-GateSlotDirectory), $followed, [StringComparison]::OrdinalIgnoreCase))"
'@ }
    'stale'         = @{ Exit = 0; Wanted = @('SLOT stale-holder=False', 'SLOT taken-over holder=gate probe'); Unwanted = @('GATE: FAILED'); Code = @'
$directory = Get-GateSlotDirectory
New-Item -ItemType Directory -Force -Path $directory | Out-Null
@{ owner = 'probe dead holder'; token = 'stale'; pid = $PID; processStartedAt = '2000-01-01T00:00:00.0000000Z'; acquiredAt = '2000-01-01T00:00:00.0000000Z' } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $directory 'gate-slot.json') -Encoding utf8
Write-Host "SLOT stale-holder=$([bool](Get-GateSlotHolder))"
$slot = Enter-GateSlotForGate 'probe'
Write-Host "SLOT taken-over holder=$((Get-GateSlotHolder).owner)"
Exit-GateSlot -Slot $slot
'@ }
    'account-wide'  = @{ Exit = 0; Wanted = @('SLOT remote-desktop-session-uses-account-root=True', 'SLOT console-session-uses-account-root=True'); Unwanted = @('=False'); Code = @'
$env:LOCALAPPDATA = Join-Path $Temp 'profile-local'
$expected = Join-Path (Join-Path $env:LOCALAPPDATA 'Temp') 'claude\crgolden-gates'
New-Item -ItemType Directory -Force -Path (Join-Path $env:LOCALAPPDATA 'Temp\2') | Out-Null
$env:TEMP = Join-Path $env:LOCALAPPDATA 'Temp\2'
$fromSession = Get-GateSlotDirectory
$env:TEMP = Join-Path $env:LOCALAPPDATA 'Temp'
$fromConsole = Get-GateSlotDirectory
Write-Host "SLOT remote-desktop-session-uses-account-root=$([string]::Equals($fromSession, $expected, [StringComparison]::OrdinalIgnoreCase))"
Write-Host "SLOT console-session-uses-account-root=$([string]::Equals($fromConsole, $expected, [StringComparison]::OrdinalIgnoreCase))"
'@ }
}

try {
    foreach ($name in $cases.Keys) {
        $case = $cases[$name]
        $temp = Join-Path $work $name
        New-Item -ItemType Directory -Force -Path $temp | Out-Null
        $child = Join-Path $work "$name.ps1"
        Set-Content -LiteralPath $child -Encoding utf8 -Value @(
            'param([string]$Module, [string]$Temp)',
            'Remove-Item Env:CLAUDE_CODE_TMPDIR, Env:CRGOLDEN_GATE_SLOT_TOKEN, Env:CRGOLDEN_GATE_SLOT_DIRECTORY -ErrorAction SilentlyContinue',
            '$env:TEMP = $Temp',
            '. $Module',
            $case.Code,
            'Write-Host REACHED')
        $printed = (& pwsh -NoProfile -File $child -Module $module -Temp $temp 2>&1 | ForEach-Object { "$_" }) -join "`n"
        $exit = $LASTEXITCODE
        $problems = @()
        if ($exit -ne $case.Exit) { $problems += "exit $exit, expected $($case.Exit)" }
        $reached = @($printed -split "`n" | ForEach-Object { $_.Trim() }) -contains 'REACHED'
        if ($reached -ne ($case.Exit -eq 0)) { $problems += "reached=$reached, expected $($case.Exit -eq 0)" }
        $problems += @($case.Wanted | Where-Object { -not $printed.Contains($_) } | ForEach-Object { "missing '$_'" })
        $problems += @($case.Unwanted | Where-Object { $printed.Contains($_) } | ForEach-Object { "unexpected '$_'" })
        if ($problems.Count) { $failures++; Write-Output "FAIL  slot-$name  $($problems -join '; ')  printed: $printed" }
        else { Write-Output "PASS  slot-$name" }
    }
} finally {
    Remove-FixtureTree $work
}
if ($failures) { Write-Output "$failures case(s) failed."; exit 1 }
Write-Output 'All cases passed.'
exit 0

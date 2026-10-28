if (-not (Get-Command Get-ClaudeAccountRoot -ErrorAction SilentlyContinue)) { . (Join-Path $PSScriptRoot '..\Hooks\ClaudeTemp.ps1') }

$script:gateSlotPollSeconds = 15
$script:gateSlotProbeAttempts = 5
$script:gateSlotProbeMilliseconds = 200
$script:errorSharingViolation = 32

function Get-GateSlotDirectory {
    return Join-Path (Get-ClaudeAccountRoot) 'crgolden-gates'
}

function Write-GateSlotRecord([string]$json) {
    $bytes = [byte[]]@()
    if ($json) { $bytes = [Text.Encoding]::UTF8.GetBytes($json) }
    $record = [IO.File]::Open((Join-Path (Get-GateSlotDirectory) 'gate-slot.json'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $record.SetLength(0)
        $record.Write($bytes, 0, $bytes.Length)
    }
    finally { $record.Dispose() }
}

function Get-GateSlotHolder {
    $sidecar = Join-Path (Get-GateSlotDirectory) 'gate-slot.json'
    if (-not (Test-Path -LiteralPath $sidecar)) { return $null }
    try { $holder = Get-Content -LiteralPath $sidecar -Raw | ConvertFrom-Json -DateKind String }
    catch { return $null }
    if ($null -eq $holder -or $null -eq $holder.pid) { return $null }
    $process = Get-Process -Id $holder.pid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if ($null -ne $process.StartTime -and $process.StartTime.ToUniversalTime().ToString('o') -ne $holder.processStartedAt) { return $null }
    return $holder
}

function Enter-GateSlot([string]$Owner, [int]$TimeoutMinutes) {
    $directory = Get-GateSlotDirectory
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $lock = Join-Path $directory 'gate-slot.lock'
    $deadline = [DateTimeOffset]::UtcNow.AddMinutes($TimeoutMinutes)
    $attempts = 0
    while ($true) {
        $attempts++
        try {
            $stream = [IO.File]::Open($lock, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            break
        }
        catch [IO.IOException] {
            if (($_.Exception.HResult -band 0xFFFF) -ne $script:errorSharingViolation) { throw }
            if ([DateTimeOffset]::UtcNow -ge $deadline -and $attempts -ge $script:gateSlotProbeAttempts) { return $null }
            if ($attempts -lt $script:gateSlotProbeAttempts) { Start-Sleep -Milliseconds $script:gateSlotProbeMilliseconds }
            else { Start-Sleep -Seconds $script:gateSlotPollSeconds }
        }
    }
    $slot = [pscustomobject]@{
        owner            = $Owner
        token            = [Guid]::NewGuid().ToString('N')
        pid              = $PID
        processStartedAt = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
        acquiredAt       = [DateTimeOffset]::UtcNow.ToString('o')
    }
    try { Write-GateSlotRecord ($slot | ConvertTo-Json) }
    catch {
        $stream.Dispose()
        throw
    }
    $slot | Add-Member -NotePropertyName Stream -NotePropertyValue $stream
    return $slot
}

function Exit-GateSlot($Slot) {
    if ($null -eq $Slot) { return }
    $holder = Get-GateSlotHolder
    if ($holder -and [string]::Equals($holder.token, $Slot.token, [StringComparison]::Ordinal)) {
        Write-GateSlotRecord $null
    }
    $Slot.Stream.Dispose()
}

function Enter-GateSlotForGate([string]$Repo, [string]$Verdict = 'GATE: FAILED') {
    $holder = Get-GateSlotHolder
    if ($holder -and -not [string]::IsNullOrWhiteSpace($env:CRGOLDEN_GATE_SLOT_TOKEN) -and
        [string]::Equals($holder.token, $env:CRGOLDEN_GATE_SLOT_TOKEN, [StringComparison]::Ordinal)) {
        Write-Host "gate slot held by the launching process: $($holder.owner)"
        return $null
    }
    try { $slot = Enter-GateSlot -Owner "gate $Repo ($env:USERNAME, session $env:CLAUDE_CODE_SESSION_ID)" -TimeoutMinutes 0 }
    catch {
        Write-Host "$Verdict (could not open this account's gate slot in $(Get-GateSlotDirectory): $($_.Exception.Message))"
        exit 1
    }
    if ($null -eq $slot) {
        $current = Get-GateSlotHolder
        $who = if ($current) { "$($current.owner), pid $($current.pid), since $($current.acquiredAt)" } else { 'a process that left no holder record' }
        Write-Host "$Verdict (this account's gate slot is held by $who; one gate at a time per account, Harness/AGENTS/GATES.md)"
        exit 1
    }
    return $slot
}

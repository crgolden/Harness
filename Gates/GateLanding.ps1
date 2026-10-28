function Test-GateRecordIsLanding {
    $script:gateLandingFile -and $script:gateRecordFile -and
        [string]::Equals([IO.Path]::GetFullPath($script:gateLandingFile), [IO.Path]::GetFullPath($script:gateRecordFile), [StringComparison]::OrdinalIgnoreCase)
}

function Get-GateSupersedingLanding {
    <#
    .SYNOPSIS
        The landing gate_status.py --record-landing wrote into this repo's main-checkout record after this gate
        started, as a hashtable, else $null. A landing means main now holds new code, so the tree this gate reads is
        outdated and its verdict would describe nothing anyone ships. Read between steps, from a file: no fetch runs.
    #>
    if (-not $script:gateLandingFile -or -not $script:gateStartedAtMs -or -not (Test-Path -LiteralPath $script:gateLandingFile -PathType Leaf)) { return $null }
    try {
        $record = Get-Content -LiteralPath $script:gateLandingFile -Raw -ErrorAction Stop | ConvertFrom-Json -AsHashtable
    } catch {
        return $null
    }
    if (-not $record -or $record['verdict'] -cne 'LANDED' -or -not ($record['landedAtMs'] -is [long] -or $record['landedAtMs'] -is [int])) { return $null }
    if ([long]$record['landedAtMs'] -le [long]$script:gateStartedAtMs) { return $null }
    $record
}

function Stop-SupersededGate($landing) {
    $head = "$($landing['head'])"
    $why = "superseded: $script:gateRepoName landed $($head.Substring(0, [Math]::Min(12, $head.Length))) on main after this gate started"
    foreach ($step in $script:gateDeclaredSteps) {
        if (-not ($script:gateReportedSteps | Where-Object { $_.StartsWith($step, [StringComparison]::Ordinal) })) {
            Add-GateRunRow $step 'NOT REACHED' $why
            Write-Host ("{0,-60} {1,-8} {2}" -f $step, 'NOT REACHED', $why)
        }
    }
    if (-not (Test-GateRecordIsLanding)) { Save-GateRun 'SUPERSEDED' }
    Exit-GateSlot -Slot $script:gateSlot
    $script:gateSlot = $null
    Write-Host "GATE: SUPERSEDED ($why; pull and gate again only if a change is still uncommitted)"
    exit 1
}

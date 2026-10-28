<#
.SYNOPSIS
    Strips /// XML documentation comment lines from C# source files under a given root.

.DESCRIPTION
    Removes every line whose trimmed content starts with /// (including blank /// lines inside a doc
    block), across all *.cs files under -Path, excluding bin/obj/generated directories. Leaves normal
    // comments, /* */ block comments, and code untouched. This is a mechanical text operation, not a
    Roslyn-aware one — it does not touch attributes, code, or non-/// comments.

.PARAMETER Path
    Root directory to process recursively.

.EXAMPLE
    .\Remove-XmlDoc.ps1 -Path '<workspace>\Identity'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Path
)

$ErrorActionPreference = 'Stop'
$excludeDirNames = @('bin', 'obj', '.git', '.vs')

function Test-Excluded([System.IO.FileInfo]$file) {
    $parts = $file.FullName.Split([System.IO.Path]::DirectorySeparatorChar)
    foreach ($p in $parts) {
        if ($excludeDirNames -contains $p) { return $true }
    }
    return $false
}

$files = Get-ChildItem -Path $Path -Recurse -File -Filter '*.cs' -ErrorAction SilentlyContinue |
    Where-Object { -not (Test-Excluded $_) }

$changedCount = 0
$removedLines = 0
foreach ($f in $files) {
    $lines = Get-Content -LiteralPath $f.FullName
    if (-not $lines) { continue }
    $kept = $lines | Where-Object { $_.TrimStart() -notmatch '^///' }
    if ($kept.Count -ne $lines.Count) {
        Set-Content -LiteralPath $f.FullName -Value $kept -Encoding UTF8
        $changedCount++
        $removedLines += ($lines.Count - $kept.Count)
    }
}

Write-Host "Changed $changedCount files, removed $removedLines /// lines under $Path"

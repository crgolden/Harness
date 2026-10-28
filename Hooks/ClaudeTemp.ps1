function ConvertTo-LongPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '~') { return $full }
    $root = [IO.Path]::GetPathRoot($full)
    $current = $root
    foreach ($segment in $full.Substring($root.Length).Split([IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
        $name = $segment
        if ($segment -match '~' -and (Test-Path -LiteralPath $current)) {
            $match = Get-ChildItem -LiteralPath $current -Filter $segment -Force -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -ne $match) { $name = $match.Name }
        }
        $current = Join-Path $current $name
    }
    return $current
}

function Get-ClaudeTempOverride {
    if ([string]::IsNullOrWhiteSpace($env:CLAUDE_CODE_TMPDIR)) { return '' }
    return $env:CLAUDE_CODE_TMPDIR
}

function Get-ClaudeRawTempRoot {
    $base = Get-ClaudeTempOverride
    if ([string]::IsNullOrWhiteSpace($base)) { $base = $env:TEMP }
    if ([string]::IsNullOrWhiteSpace($base)) { $base = [IO.Path]::GetTempPath() }
    return Join-Path (ConvertTo-LongPath $base) 'claude'
}

function Get-ClaudeShareConfig([string]$Path = (Join-Path $PSScriptRoot 'state_share.json')) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $null }
}

function Get-ClaudeShareRoot($Config = (Get-ClaudeShareConfig), [string]$Machine = $env:COMPUTERNAME, [string]$User = $env:USERNAME) {
    if ($null -eq $Config -or [string]::IsNullOrWhiteSpace($Machine)) { return $null }
    if (-not (@($Config.enabled) | Where-Object { [string]::Equals([string]$_, $Machine, [StringComparison]::OrdinalIgnoreCase) })) { return $null }
    if (-not (@($Config.users) | Where-Object { [string]::Equals([string]$_, $User, [StringComparison]::OrdinalIgnoreCase) })) { return $null }
    $base = if ([string]::Equals([string]$Config.server, $Machine, [StringComparison]::OrdinalIgnoreCase)) { [string]$Config.serverPath }
            elseif ($Config.drive) { "$(([string]$Config.drive).TrimEnd(':')):\" } else { '' }
    if ([string]::IsNullOrWhiteSpace($base)) { return $null }
    return $base
}

function Get-ClaudeCutoverFolder($Config = (Get-ClaudeShareConfig), [string]$Machine = $env:COMPUTERNAME, [string]$User = $env:USERNAME) {
    $top = Get-ClaudeShareRoot $Config $Machine $User
    if ([string]::IsNullOrWhiteSpace($top)) { return $null }
    return Join-Path (Join-Path $top 'cutover') "$($Machine.ToUpperInvariant()).$($User.ToLowerInvariant())"
}

function Get-ClaudeLocalMarkerLayout([string]$RawRoot = (Get-ClaudeRawTempRoot)) {
    $local = Join-Path $RawRoot 'share-cutover.json'
    if (-not (Test-Path -LiteralPath $local -PathType Leaf)) { return $null }
    try { $data = Get-Content -LiteralPath $local -Raw | ConvertFrom-Json } catch { return 'unknown' }
    if ($null -eq $data -or $data -isnot [pscustomobject]) { return 'unknown' }
    $layout = $data.PSObject.Properties['layout']
    if ($null -eq $layout -or [string]::IsNullOrWhiteSpace([string]$layout.Value)) { return 'per-machine' }
    return [string]$layout.Value
}

function Test-ClaudeShareCutOver([string]$Folder, [string]$RawRoot = (Get-ClaudeRawTempRoot), [string]$Layout = 'flat') {
    if ([string]::IsNullOrWhiteSpace($Folder)) { return $false }
    $found = Get-ClaudeLocalMarkerLayout $RawRoot
    if ($found -eq $Layout) { return $true }
    if (-not (Test-Path -LiteralPath (Join-Path $Folder 'cutover.json') -PathType Leaf)) {
        return ($found -eq 'unknown' -and $Layout -eq 'flat' -and -not (Test-Path -LiteralPath (Split-Path (Split-Path $Folder)) -PathType Container))
    }
    if ($found -ne 'flat') {
        try {
            New-Item -ItemType Directory -Force -Path $RawRoot -ErrorAction Stop | Out-Null
            @{ share = $Folder; layout = $Layout } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RawRoot 'share-cutover.json') -Encoding utf8 -ErrorAction Stop
        }
        catch { }
    }
    return $true
}

function Get-ClaudeLegacyFolder($Config = (Get-ClaudeShareConfig), [string]$Machine = $env:COMPUTERNAME, [string]$User = $env:USERNAME) {
    $top = Get-ClaudeShareRoot $Config $Machine $User
    if ([string]::IsNullOrWhiteSpace($top)) { return $null }
    return Join-Path (Join-Path $top $Machine.ToUpperInvariant()) $User.ToLowerInvariant()
}

function Test-ClaudeAccountTemp([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA) -or [string]::IsNullOrWhiteSpace($Path)) { return $false }
    $base = (ConvertTo-LongPath (Join-Path $env:LOCALAPPDATA 'Temp')).TrimEnd('\', '/')
    $full = (ConvertTo-LongPath $Path).TrimEnd('\', '/')
    if ([string]::Equals($full, $base, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return [string]::Equals((Split-Path $full), $base, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path $full -Leaf) -match '^\d+$'
}

function Get-ClaudeTempRoot {
    if (-not (Test-ClaudeAccountTemp (Split-Path (Get-ClaudeRawTempRoot)))) { return Get-ClaudeRawTempRoot }
    $top = Get-ClaudeShareRoot
    if (-not $top) { return Get-ClaudeRawTempRoot }
    if (Test-ClaudeShareCutOver (Get-ClaudeCutoverFolder)) { return Join-Path $top 'claude' }
    $legacy = Get-ClaudeLegacyFolder
    if (Test-ClaudeShareCutOver $legacy -Layout 'per-machine') { return Join-Path $legacy 'claude' }
    return Get-ClaudeRawTempRoot
}

function Get-ClaudeAccountRoot {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA) -or -not (Test-ClaudeAccountTemp (Split-Path (Get-ClaudeRawTempRoot)))) {
        return Get-ClaudeRawTempRoot
    }
    return Join-Path (ConvertTo-LongPath (Join-Path $env:LOCALAPPDATA 'Temp')) 'claude'
}

function Test-ClaudeFlatState {
    if (-not (Test-ClaudeAccountTemp (Split-Path (Get-ClaudeRawTempRoot)))) { return $false }
    if (-not (Get-ClaudeShareRoot)) { return $false }
    return [bool](Test-ClaudeShareCutOver (Get-ClaudeCutoverFolder))
}

function Get-CheckoutKey([string]$Checkout, [string]$Machine = $env:COMPUTERNAME, $Flat = $null) {
    $full = (ConvertTo-LongPath $Checkout).TrimEnd('\', '/').ToLowerInvariant()
    $prefixed = if ($null -ne $Flat) { [bool]$Flat } else { Test-ClaudeFlatState }
    if ($prefixed) { $full = "$($Machine.ToLowerInvariant())-$full" }
    return [regex]::Replace($full, '[^a-z0-9]', '-')
}

function Get-GateRecordsDirectory([string]$Checkout) {
    return Join-Path (Join-Path (Get-ClaudeTempRoot) 'crgolden-gates\records') (Get-CheckoutKey $Checkout)
}

function Remove-ClaudeTempTree([string]$Path) {
    <#
    .SYNOPSIS
        Deletes a directory tree with POSIX delete semantics, so each name leaves its directory at once even while
        another process (a scanner) still holds the file open; a plain delete leaves a pending entry and the parent's
        removal fails "The directory is not empty". Never throws: it returns the paths still present afterwards,
        empty when the tree is gone, and a caller must assert on that list, since a failed unlink shows only there.
    #>
    if (-not ('ClaudeTempTree' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class ClaudeTempTree {
    const uint DeleteAccess = 0x00010000, ShareAll = 7, OpenExisting = 3, BackupSemantics = 0x02000000, OpenReparsePoint = 0x00200000;
    const int FileDispositionInfoEx = 21;
    const uint DispositionDelete = 0x1, PosixSemantics = 0x2, IgnoreReadonly = 0x10;
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, ref uint info, uint size);
    [DllImport("kernel32.dll", SetLastError = true, EntryPoint = "SetFileInformationByHandle")]
    static extern bool SetFileDisposition(SafeFileHandle handle, int infoClass, ref byte info, uint size);
    public static void UnlinkLegacy(string path) {
        using (var handle = CreateFileW(path, DeleteAccess, ShareAll, IntPtr.Zero, OpenExisting, BackupSemantics | OpenReparsePoint, IntPtr.Zero)) {
            if (handle.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error(), path); }
            byte deleteFile = 1;
            if (!SetFileDisposition(handle, 4, ref deleteFile, 1)) { throw new Win32Exception(Marshal.GetLastWin32Error(), path); }
        }
    }
    static void Unlink(string path) {
        using (var handle = CreateFileW(path, DeleteAccess, ShareAll, IntPtr.Zero, OpenExisting, BackupSemantics | OpenReparsePoint, IntPtr.Zero)) {
            if (handle.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error(), path); }
            uint flags = DispositionDelete | PosixSemantics | IgnoreReadonly;
            if (!SetFileInformationByHandle(handle, FileDispositionInfoEx, ref flags, 4)) { throw new Win32Exception(Marshal.GetLastWin32Error(), path); }
        }
    }
    public static void Remove(string root) {
        var info = new DirectoryInfo(root);
        if (!info.Exists) { return; }
        if ((info.Attributes & FileAttributes.ReparsePoint) == 0) {
            foreach (var entry in info.EnumerateFileSystemInfos()) {
                if (entry is DirectoryInfo) { Remove(entry.FullName); } else { Unlink(entry.FullName); }
            }
        }
        Unlink(root);
    }
}
'@
    }
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try { [ClaudeTempTree]::Remove($Path) } catch { }
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    return @(Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object FullName) + @($Path)
}

function Remove-FixtureTree([string]$Path) {
    <#
    .SYNOPSIS
        A harness's teardown of its fixture folder: Remove-ClaudeTempTree, then a throw naming what is left, so a
        fixture that outlives its run fails the harness instead of piling up in the temp root unseen.
    #>
    $left = @(Remove-ClaudeTempTree $Path)
    if ($left.Count) { throw "fixture teardown left $($left.Count) path(s) under ${Path}: $($left[0])" }
}

function New-ClaudeTempPath([string]$Name) {
    $root = Get-ClaudeRawTempRoot
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    return Join-Path $root $Name
}

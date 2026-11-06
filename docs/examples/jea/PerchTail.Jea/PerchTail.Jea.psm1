# PerchTail read-only JEA role (#169): the only commands a PerchTail WinRM
# source can run on this host when it connects to the PerchTail JEA endpoint.
#
# Every function:
# - accepts only paths under the roots listed in
#   %ProgramData%\PerchTail\jea-roots.txt (one folder per line);
# - refuses a path that goes through a junction or symbolic link below a
#   root, without following it;
# - only reads, opening files with FileShare.ReadWrite|Delete so a log a
#   service is still writing can be read;
# - returns text (base64 where it carries bytes), never live objects.
#
# The session runs in NoLanguage mode: the caller can run these functions
# with parameters and nothing else. They run as the connecting account, so
# its file permissions still apply.

Set-StrictMode -Version 3.0

$script:RootsFile = Join-Path $env:ProgramData 'PerchTail\jea-roots.txt'
$script:MaxBlock = 4MB
$script:MaxRange = 1MB
$script:MaxHead = 4KB

function Get-PerchTailAllowedRoots {
    if (-not (Test-Path -LiteralPath $script:RootsFile)) { return @() }
    Get-Content -LiteralPath $script:RootsFile |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith('#') } |
        ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\') }
}

# The full path, if it's under an allowed root and nothing between the root
# and it is a junction or symbolic link; otherwise a terminating error that
# PerchTail reads as "outside the base path".
function Resolve-PerchTailPath([string] $Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'PERCHTAIL_OUTSIDE_BASE' }
    # A colon past the drive letter would be an alternate data stream.
    if ($Path.IndexOf(':', 2) -ge 0 -or $Path.StartsWith('\\?\')) { throw 'PERCHTAIL_OUTSIDE_BASE' }
    try { $full = [IO.Path]::GetFullPath($Path).TrimEnd('\') } catch { throw 'PERCHTAIL_OUTSIDE_BASE' }
    if ($full.Length -eq 2 -and $full[1] -eq ':') { $full += '\' }

    foreach ($root in Get-PerchTailAllowedRoots) {
        $rootKey = if ($root.Length -eq 2) { "$root\" } else { $root }
        $inside = [string]::Equals($full, $rootKey, [StringComparison]::OrdinalIgnoreCase) -or
            $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
        if (-not $inside) { continue }
        # Each component below the root: its own attributes, not its target's.
        $current = $root
        foreach ($part in $full.Substring($root.Length).Split('\', [StringSplitOptions]::RemoveEmptyEntries)) {
            $current = "$current\$part"
            if (Test-Path -LiteralPath $current) {
                if ([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) {
                    throw 'PERCHTAIL_OUTSIDE_BASE'
                }
            }
        }
        return $full
    }
    throw 'PERCHTAIL_OUTSIDE_BASE'
}

function Open-PerchTailFile([string] $Full) {
    if (-not [IO.File]::Exists($Full)) { throw 'PERCHTAIL_NOT_FOUND' }
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    [IO.File]::Open($Full, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
}

function Read-PerchTailBytes($Stream, [long] $Offset, [int] $Count) {
    $buffer = New-Object byte[] $Count
    $total = 0
    if ($Offset -lt $Stream.Length -and $Count -gt 0) {
        $null = $Stream.Seek($Offset, [IO.SeekOrigin]::Begin)
        while ($total -lt $Count) {
            $read = $Stream.Read($buffer, $total, $Count - $total)
            if ($read -le 0) { break }
            $total += $read
        }
    }
    [Convert]::ToBase64String($buffer, 0, $total)
}

<#
.SYNOPSIS
The folders PerchTail may read, as base64 UTF-8 JSON.
#>
function Get-PerchTailRoots {
    $json = ConvertTo-Json -Compress -InputObject @(Get-PerchTailAllowedRoots)
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

<#
.SYNOPSIS
A folder's entries (Name, PSIsContainer, Length, MTime in Unix ms), leaving
out junctions and links, as base64 UTF-8 JSON.
#>
function Get-PerchTailListing {
    param([Parameter(Mandatory)] [string] $Path)
    $full = Resolve-PerchTailPath $Path
    if (-not [IO.Directory]::Exists($full)) { throw 'PERCHTAIL_NOT_A_DIRECTORY' }
    $entries = foreach ($item in [IO.DirectoryInfo]::new($full).EnumerateFileSystemInfos()) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $isDir = [bool]($item.Attributes -band [IO.FileAttributes]::Directory)
        [ordered]@{
            Name          = $item.Name
            PSIsContainer = $isDir
            Length        = if ($isDir) { 0 } else { $item.Length }
            MTime         = [DateTimeOffset]::new($item.LastWriteTimeUtc).ToUnixTimeMilliseconds()
        }
    }
    $json = ConvertTo-Json -Compress -Depth 2 -InputObject @($entries)
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

<#
.SYNOPSIS
A file's size in bytes.
#>
function Get-PerchTailSize {
    param([Parameter(Mandatory)] [string] $Path)
    $full = Resolve-PerchTailPath $Path
    if (-not [IO.File]::Exists($full)) { throw 'PERCHTAIL_NOT_FOUND' }
    [string] [IO.FileInfo]::new($full).Length
}

<#
.SYNOPSIS
Up to Length bytes (4 MiB at most) from Offset, as base64.
#>
function Read-PerchTailBlock {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateRange(0, [long]::MaxValue)] [long] $Offset,
        [Parameter(Mandatory)] [ValidateRange(0, [int]::MaxValue)] [int] $Length
    )
    $full = Resolve-PerchTailPath $Path
    $stream = Open-PerchTailFile $full
    try { Read-PerchTailBytes $stream $Offset ([Math]::Min($Length, $script:MaxBlock)) }
    finally { $stream.Dispose() }
}

<#
.SYNOPSIS
For PerchTail's incremental follow: "size|head|data", with the file's first
HeadLength bytes (4 KiB at most) and up to Length bytes (1 MiB at most) from
Offset, both base64.
#>
function Read-PerchTailRange {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateRange(0, [long]::MaxValue)] [long] $Offset,
        [Parameter(Mandatory)] [ValidateRange(0, [int]::MaxValue)] [int] $Length,
        [ValidateRange(0, [int]::MaxValue)] [int] $HeadLength = 0
    )
    $full = Resolve-PerchTailPath $Path
    $stream = Open-PerchTailFile $full
    try {
        $size = $stream.Length
        $head = Read-PerchTailBytes $stream 0 ([Math]::Min($HeadLength, $script:MaxHead))
        $data = Read-PerchTailBytes $stream $Offset ([Math]::Min($Length, $script:MaxRange))
        "$size|$head|$data"
    }
    finally { $stream.Dispose() }
}

Export-ModuleMember -Function Get-PerchTailRoots, Get-PerchTailListing, Get-PerchTailSize,
    Read-PerchTailBlock, Read-PerchTailRange

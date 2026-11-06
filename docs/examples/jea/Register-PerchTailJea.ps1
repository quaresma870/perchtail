#Requires -RunAsAdministrator
<#
.SYNOPSIS
Installs PerchTail's read-only JEA endpoint (#169) on this host.

.DESCRIPTION
- Copies the PerchTail.Jea module (with its PerchTailReader role) to
  %ProgramFiles%\WindowsPowerShell\Modules.
- Writes the folders PerchTail may read to %ProgramData%\PerchTail\jea-roots.txt
  (readable by everyone, writable by Administrators only).
- Registers the session configuration, letting the given group connect.

Run it again to change the roots or the group. Remove the endpoint with
  Unregister-PSSessionConfiguration -Name PerchTail

.EXAMPLE
.\Register-PerchTailJea.ps1 -Group 'CONTOSO\PerchTail Readers' -Roots 'D:\Logs', 'C:\inetpub\logs'
#>
param(
    # The group (or account) PerchTail's WinRM credential belongs to. It
    # doesn't need to be an administrator or in Remote Management Users.
    [Parameter(Mandatory)] [string] $Group,
    # The folders PerchTail may list and read. A source's base path must be
    # one of them, or inside one.
    [Parameter(Mandatory)] [string[]] $Roots,
    # The endpoint name to put in the source's "JEA endpoint" field.
    [string] $Name = 'PerchTail'
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

foreach ($root in $Roots) {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "Not a folder: $root" }
}

$modules = Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules\PerchTail.Jea'
if (Test-Path $modules) { Remove-Item $modules -Recurse -Force }
Copy-Item -Path (Join-Path $here 'PerchTail.Jea') -Destination $modules -Recurse

$dataDir = Join-Path $env:ProgramData 'PerchTail'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
# Only administrators can change which folders PerchTail may read: the
# folder is owned by Administrators (a user who created it first would keep
# the owner's right to change its permissions) and locked down before the
# roots file is written into it.
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner([Security.Principal.NTAccount] 'BUILTIN\Administrators')
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @(
    @('BUILTIN\Administrators', 'FullControl'),
    @('NT AUTHORITY\SYSTEM', 'FullControl'),
    @('BUILTIN\Users', 'ReadAndExecute')
)) {
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $rule[0], $rule[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
}
Set-Acl -LiteralPath $dataDir -AclObject $acl
$rootsFile = Join-Path $dataDir 'jea-roots.txt'
# Written afresh, so it takes the folder's permissions rather than keeping
# whoever owned an existing one.
Remove-Item -LiteralPath $rootsFile -Force -ErrorAction SilentlyContinue
$Roots | ForEach-Object { [IO.Path]::GetFullPath($_) } | Set-Content -LiteralPath $rootsFile -Encoding UTF8

$pssc = Join-Path $env:TEMP "PerchTail-$([guid]::NewGuid()).pssc"
(Get-Content -LiteralPath (Join-Path $here 'PerchTail.pssc') -Raw).Replace(
    "'PERCHTAIL_GROUP'", "'" + $Group.Replace("'", "''") + "'") | Set-Content -LiteralPath $pssc
try {
    if (Get-PSSessionConfiguration -Name $Name -ErrorAction SilentlyContinue) {
        Unregister-PSSessionConfiguration -Name $Name -Force -NoServiceRestart
    }
    Register-PSSessionConfiguration -Name $Name -Path $pssc -Force | Out-Null
}
finally {
    Remove-Item -LiteralPath $pssc -ErrorAction SilentlyContinue
}

Write-Host "PerchTail JEA endpoint '$Name' registered for $Group, reading:"
$Roots | ForEach-Object { Write-Host "  $_" }
Write-Host "In PerchTail, set the WinRM source's JEA endpoint to '$Name'."

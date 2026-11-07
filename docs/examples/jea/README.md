# PerchTail read-only JEA endpoint

A Just Enough Administration (JEA) endpoint for PerchTail's WinRM sources
(#169). With it, the account PerchTail uses needs **no administrator rights
and no remote shell**: it can call five read-only functions, and nothing
else.

| Function | What it does |
|---|---|
| `Get-PerchTailRoots` | The folders PerchTail may read |
| `Get-PerchTailListing -Path` | A folder's files and subfolders (name, size, last write) |
| `Get-PerchTailSize -Path` | A file's size |
| `Read-PerchTailBlock -Path -Offset -Length` | Up to 4 MiB of a file |
| `Read-PerchTailRange -Path -Offset -Length -HeadLength` | A range and the file's start, for Tail (incremental follow) |

Every function:

- accepts only paths under the folders in
  `%ProgramData%\PerchTail\jea-roots.txt`, after resolving `..`;
- refuses a path that goes through a junction or symbolic link below one of
  those folders, without following it, and lists none;
- only reads: files are opened with `FileShare.ReadWrite | Delete`, so a log
  a service is still writing can be read, and nothing is ever written;
- runs as the connecting account (no virtual account), so that account's own
  NTFS permissions still apply.

The endpoint runs in `NoLanguage` mode (`RestrictedRemoteServer`): PerchTail
calls the functions by name, with typed parameters, and can't run anything
else.

## Install

On the Windows host, as an administrator, from a copy of this folder:

```powershell
.\Register-PerchTailJea.ps1 -Group 'CONTOSO\PerchTail Readers' -Roots 'D:\Logs', 'C:\inetpub\logs'
```

- `-Group`: the group (or account) of PerchTail's WinRM credential. It needs
  no other rights: not Administrators, not Remote Management Users, not the
  WinRM `RootSDDL`.
- `-Roots`: the folders PerchTail may read. A source's **Base path** must be
  one of them or inside one.
- `-Name` (default `PerchTail`): the endpoint name to put in the source's
  **JEA endpoint** field.

Run it again to change the folders or the group. Remove it with:

```powershell
Unregister-PSSessionConfiguration -Name PerchTail
Remove-Item "$env:ProgramFiles\WindowsPowerShell\Modules\PerchTail.Jea" -Recurse
```

## In PerchTail

Edit the WinRM source, set **JEA endpoint** to `PerchTail` (or the `-Name`
you chose), keep **Scheme** on HTTPS with the listener's CA certificate if
needed, and use **Check**. The Base path picker then offers the allowed
folders.

## Files

- `PerchTail.Jea/PerchTail.Jea.psm1` -- the functions.
- `PerchTail.Jea/RoleCapabilities/PerchTailReader.psrc` -- the role: those
  functions and nothing else visible.
- `PerchTail.pssc` -- the endpoint: `NoLanguage`, no virtual account, the role
  for the given group. Transcripts are off, since they would record file
  contents; turn them on in this file if you want them.
- `Register-PerchTailJea.ps1` -- installs the module, writes the roots file
  (changeable by administrators only) and registers the endpoint.

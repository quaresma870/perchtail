@{
    RootModule        = 'PerchTail.Jea.psm1'
    ModuleVersion     = '0.6.0'
    GUID              = '7b0c6a51-3f8e-4d2a-9b61-2d4f6e8a1c35'
    Author            = 'PerchTail'
    Description       = 'Read-only commands for a PerchTail WinRM source behind a JEA endpoint (#169).'
    PowerShellVersion = '5.1'
    FunctionsToExport = @(
        'Get-PerchTailRoots',
        'Get-PerchTailListing',
        'Get-PerchTailSize',
        'Read-PerchTailBlock',
        'Read-PerchTailRange'
    )
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
}

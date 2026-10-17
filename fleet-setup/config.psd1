@{
    # Everything a new fleet PC gets. Edit here, not in Setup-FleetPC.ps1.

    # winget IDs, installed in order. Checked 2026-10-17 with `winget show --id <id> -e`.
    Packages = @(
        'Git.Git'
        'GitHub.cli'
        'OpenJS.NodeJS.LTS'
        'Python.Python.3.14'
        'Microsoft.PowerShell'
        'Microsoft.DotNet.SDK.10'
        'Google.GoogleDrive'
        'Anthropic.Claude'
    )

    # Asked yes/no during setup.
    OptionalPackages = @(
        'Tailscale.Tailscale'
    )

    # Buzz Desktop is not on winget. Pinned to the version White and Plexy run.
    # Digest from the GitHub release asset metadata (gh api .../releases/tags/desktop-v0.5.28).
    Buzz = @{
        Version = '0.5.28'
        Url     = 'https://github.com/block/buzz/releases/download/desktop-v0.5.28/Buzz_0.5.28_x64-setup_alpha-unsigned.exe'
        Sha256  = 'dfecae208e81f6606efeacfb476e9947c9c14dfc6ba992742c57bb469d594b5d'
    }

    ClaudeCodeInstaller = 'https://claude.ai/install.ps1'

    # Claude profile: CLAUDE.md, skills, hooks, settings, MCP config. Path must have no spaces.
    ProfileRepo = 'https://github.com/YourBIMpossible/claude-profile.git'
    ProfileDir  = 'C:\claude-profile'

    # Repos cloned into RepoRoot. A repo the signed-in gh account cannot reach is skipped with a warning.
    RepoOwner = 'YourBIMpossible'
    RepoRoot  = 'C:\src'
    Repos     = @(
        'claude-tools'
        'team-claude'
        'BIMpossible-AddIns'
        'BIMpossible'
        'BIMpossible_Workspace'
    )
}

# Fleet PC setup kit (USB)

One pass sets up a new fleet PC the way Zen and Plexy were set up. It holds no secrets: every sign-in is typed by you in the window the script opens.

## Use

1. Copy this folder (or unzip `FleetSetup-kit.zip`) to a USB stick.
2. On the new PC, signed in as the account you will use daily, which must be a local admin: double-click **`Start-Setup.cmd`** and accept UAC.
3. Answer the prompts: PC name, optional Tailscale, the sign-ins (GitHub in a browser, Claude, Google Drive, Tailscale). Quit Claude when the script asks, because the profile bootstrap needs it closed.
4. Reboot when the script asks.
5. Open Claude Code with the working folder `C:\src` and paste the prompt from `<NAME>-CLAUDE-PROMPT.md` on the Desktop. It finishes the profile plugins, the repo hooks and the Quad worker, then stops at the Buzz identity step for you.

Checks only, no changes: `Start-Setup.cmd -DryRun`.
Re-running is safe: steps already done are skipped.

## What it does

| Step | How |
|---|---|
| PC name | `Rename-Computer`, applied at reboot |
| Remote Desktop | RDP on, NLA on, firewall rules (Pro edition needed) |
| Apps | winget: Git, GitHub CLI, Node LTS, Python 3.14, PowerShell 7, .NET SDK 10, Google Drive, Claude desktop; Tailscale optional |
| Claude Code CLI | official `claude.ai/install.ps1` |
| User PATH | adds `%APPDATA%\npm`, `%USERPROFILE%\.local\bin` |
| Buzz Desktop 0.5.28 | downloaded from the block/buzz GitHub release and checked against a pinned SHA-256 before install |
| Sign-ins | `gh auth login --web`, `claude auth login`, Drive tray sign-in, `tailscale up` |
| Claude profile | clones `claude-profile` to `C:\claude-profile`, runs `bootstrap.ps1` |
| Repos | clones the list in `config.psd1` into `C:\src` and skips any the GitHub account can't reach |
| Follow-up prompt | writes `<NAME>-CLAUDE-PROMPT.md` to the Desktop |

Logs: `C:\ProgramData\FleetSetup\setup-*.log` on the PC, plus a summary in this kit's `logs\` folder.

## Edit

Apps, the Buzz version and hash, and the repo list all live in `config.psd1`. Get a new Buzz hash with `gh api repos/block/buzz/releases/tags/desktop-v<ver> --jq '.assets[] | select(.name|endswith("x64-setup_alpha-unsigned.exe")) | .digest'`.

## Not automated, on purpose

- **Buzz identity key and relay admission.** Create the key on the new PC; it is never copied between machines. No written Windows procedure exists yet.
- **Live Quad A1 run.** You post it in the Testing channel.
- **VS Build Tools and Inno Setup.** Only needed if the PC builds BIMpossible-AddIns.
- **Git author identity.** It is set per repo during the Claude session, never globally.

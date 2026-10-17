Paste everything below the line into a Claude Code session on {{MACHINE}}, working folder C:\src.

---

You are finishing setup on this PC, "{{MACHINE}}", a machine in my fleet (White, Zen, MyBuddy, Plexy, Lazy). It gets the same setup Zen and Plexy got. The USB setup kit already did: computer name, Remote Desktop + NLA + firewall, winget apps (git, gh, node LTS, python 3.14, pwsh 7, .NET SDK 10, Google Drive, Claude desktop, optional Tailscale), Claude Code CLI, user PATH, Buzz Desktop 0.5.28, sign-ins (gh, claude, Drive, Tailscale), the claude-profile bootstrap at C:\claude-profile, and repo clones into C:\src. Its log is in C:\ProgramData\FleetSetup.

Rules for this session:
- Do not change PATH, firewall, RDP, accounts, ACLs, Tailscale, or logins - those are mine. If one is wrong, report it with the exact command for me to run.
- Never print, copy, or move secrets (tokens, Buzz keys, .credentials.json). Report key/credential presence as metadata only.
- Git identity per repo only (`git config user.name/user.email` inside the clone), never `--global`. Resolve default branches via `origin/HEAD`, never hardcode main/master.
- Quad worker files go under %USERPROFILE%\buzz-pilot and %USERPROFILE%\buzz-tasks. Do not write under %APPDATA%/%LOCALAPPDATA% from this session - the desktop app redirects those writes.
- PowerShell 5.1 trap: `2>&1` on a native command under `$ErrorActionPreference='Stop'` throws and loses the exit code; use pwsh 7 or Start-Process redirection.
- Act end-to-end; stop only at the OWNER gates below. Final report: Done / Assumed / Left-Flags, written to C:\src\{{MACHINE}}-setup\SETUP-REPORT.md.

Phase 1 - read-only check. Read the newest log in C:\ProgramData\FleetSetup and list any FAIL/WARN/SKIP. Then record: hostname; Windows edition; `claude --version`; `claude auth status` (loggedIn, authMethod, subscriptionType only); git, gh, node, npm, python (and its path), pwsh, dotnet SDKs; `gh auth status` (account name only); `tailscale status` (up/down); RDP registry values and firewall rule state; env var NAMES matching ANTHROPIC_* / CLAUDE_CODE_USE_*; `%LOCALAPPDATA%\Buzz\buzz-acp.exe` and `buzz.exe` presence + version; bootstrap output (~/.claude/CLAUDE.md, skills junction, hooks, settings.json). Anything missing -> owner fix command in the report; continue with what works.

Phase 2 - Claude profile finish. Tell me to type: `/plugin marketplace add C:\claude-profile\plugins`, `/plugin install roadmap-band@owner-local`, then `/plugin list` and `/context`.

Phase 3 - repos. In each C:\src clone: `git remote set-head origin -a`; for BIMpossible repos run `Install-Developer-Setup.ps1` if present. Report repos the kit skipped for access. Ask me which git name/email this PC commits as and set it per repo.

Phase 4 - Quad worker staging (Buzz receive path; same package as Zen/Plexy/White):
1. `git -c core.autocrlf=false clone --branch claude/buzz-agent-team-session-6d384f --single-branch --no-tags https://github.com/YourBIMpossible/team-claude.git C:\src\team-claude-pilot`, then `git -C C:\src\team-claude-pilot checkout --detach 13eab15`. Folder: `buzz/pilot-tcb02/worker/`.
2. SHA-256 (Get-FileHash), LF bytes:
   - .mcp.windows.json  0ccc3fbb5aeb383cfa8f181f4722cbd24bfacfe41d7fb367559e06860e2260b4
   - bin/pilot_mcp.py   f46888ec3cb3ad14731ae87e4afabefa3fcb30ce48906cc9705ebb262e662688
   - system-prompt.md   90f6c51ec18f57914ad3504a0fd5321f6fd0eb37872a61530526eb06fd7dccf0
   - verify_session.py  4ced59cfc4744c9278270aa49d4e544ec9dcc42a2d9b82efa9dc457860107d75
   - .claude/settings.json  starts 5c6326d0, ends 1b6f
   - run-worker.ps1         starts 9a59c5c8, ends 16bb
   Any mismatch -> stop Phase 4 and report actual hashes.
3. Adapter, per-user prefix, never -g:
   `npm install --prefix "$env:USERPROFILE\buzz-pilot\claude-agent-acp-0.90.0" --omit=optional --ignore-scripts --no-audit --no-fund @agentclientprotocol/claude-agent-acp@0.90.0`
4. Copy `buzz-acp.exe` and `buzz.exe` from `%LOCALAPPDATA%\Buzz` to `%USERPROFILE%\buzz-pilot\bin\` (keeps Desktop's updater from replacing them). Record SHA-256; White/Plexy 0.5.28: buzz-acp 85734917...86e0, buzz 2adeb6a7...bb36.
5. Task folder `%USERPROFILE%\buzz-tasks\tcb02`: `.claude\settings.json`, `.mcp.windows.json` -> `.mcp.json` with `command` = local python path, `bin\pilot_mcp.py`, `system-prompt.md`, `run-worker.ps1`, `verify_session.py`. Keep `github@synced` disabled. Record the edited .mcp.json hash.
6. OWNER: in a normal terminal, `cd $env:USERPROFILE\buzz-tasks\tcb02; claude`, accept trust for that folder only, `/exit`.
7. Keyless dry run with `$env:ENABLE_CLAUDEAI_MCP_SERVERS='false'; $env:PILOT_DRY_RUN='1'`: the `claude -p ... --setting-sources user,project,local --output-format stream-json --verbose --max-turns 4` command from run-worker.ps1 / the package README. Pass = exactly the two `mcp__pilot__*` tools and observe hash d1c49140...a2fa.
8. OWNER GATE - Buzz identity. This PC needs a NEW identity created here; never reuse another machine's key. No Windows key-generation procedure is recorded - do not invent one. Stop and report: "ready for owner: create the Buzz key at %LOCALAPPDATA%\buzz-pilot\<name>.key (ACL to this account), display name `<Name> (Claude)`, admit it to wss://bimpossible.communities.buzz.xyz and the Testing channel." After I confirm, run `.\run-worker.ps1 -Attempt A1 -RunId <id> -KeyFile <key> -Harness $env:USERPROFILE\buzz-pilot\bin\buzz-acp.exe -BuzzCli $env:USERPROFILE\buzz-pilot\bin\buzz.exe -CheckOnly` (add -AllowBuzzDesktop if Desktop is open) - must print "preconditions passed". I post the live A1 in Testing; verify with `python -I verify_session.py $T <sessionId>` (must PASS). EnterPlanMode/ExitPlanMode offered to the worker = restriction failure. Known harmless: adapter 0.90.0 forces `default` mode; one relay 403 per turn for kind 44200. Rollback: delete the adapter prefix, %USERPROFILE%\buzz-pilot\bin, and the task folder.

Phase 5 - coordination. If `G:\My Drive\Coord-Desktop\AI\Claude Collab\` is visible, create `from-{{MACHINE}}\` (lowercase) for results; no secrets, addresses, e-mails or usernames there. If C:\src\team-claude has `scripts\read-only\Get-WorkerInventory.ps1`, ask me for the worker id and run it with `powershell.exe -NoProfile -Command "& '<path>' -WorkerId <id> -OutFile '<path>' -Force"` (-Command, not -File). Report that `Test-LanePreflight.ps1`'s host guard needs a {{MACHINE}} entry - do not edit it here.

Never bring over from other machines: ~/.claude/.credentials.json, gh/GCM tokens, any Buzz *.key, White's managed-agents.json / custom_harnesses / Ollama tunnel task / start-sari.ps1, ~/.claude/scheduled-tasks, any .env, other machines' hostnames in guards.

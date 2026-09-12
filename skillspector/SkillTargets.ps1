<#
    Shared target discovery for the SkillSpector watch.

    Dot-source this file to get Get-SkillTargets.

    WHY ONE SCAN PER SKILL (this is not an accident):

    SkillSpector's --baseline flag is SILENTLY IGNORED when --recursive is
    used. Verified 2026-07-25 on v2.4.4: a recursive scan of 17 skills with a
    valid baseline reported suppressed=0 for every skill and retained all 119
    findings. The two flags do not compose.

    The reverse -- scanning a parent directory WITHOUT --recursive -- does
    honour the baseline, but only analyses the directory as a single skill. A
    deliberately malicious skill planted in that directory was NOT detected and
    the run reported CLEAN. That is the dangerous failure mode: a green result
    that proves nothing.

    So neither single call works. Instead we enumerate every skill directory
    ourselves and issue one non-recursive scan per skill, each with its own
    baseline. Baseline suppression works, and a newly added skill has no
    baseline file, which we surface as drift rather than skipping.
#>

function Get-SkillTargets {
    [CmdletBinding()]
    param(
        [string]$SkillsRoot  = (Join-Path $env:USERPROFILE '.claude\skills'),
        [string]$PluginCache = (Join-Path $env:USERPROFILE '.claude\plugins\cache')
    )

    $targets = @()

    # Personal skills: ~/.claude/skills/<skill>/SKILL.md
    if (Test-Path $SkillsRoot) {
        Get-ChildItem $SkillsRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            if (Test-Path (Join-Path $_.FullName 'SKILL.md')) {
                $targets += [pscustomobject]@{
                    Scope = 'skills'
                    Name  = $_.Name
                    Path  = $_.FullName
                }
            }
        }
    }

    # Plugin skills: ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    # The <version> segment changes on every plugin update, so it is discovered
    # rather than hardcoded, and the baseline is keyed on <plugin>/<skill> so a
    # version bump does not orphan it.
    if (Test-Path $PluginCache) {
        Get-ChildItem $PluginCache -Recurse -Filter 'SKILL.md' -ErrorAction SilentlyContinue | ForEach-Object {
            # <marketplace>\<plugin>\<version>\skills\<skill>\SKILL.md
            $skillDir   = Split-Path $_.FullName -Parent   # ...\skills\<skill>
            $skillsDir  = Split-Path $skillDir   -Parent   # ...\<version>\skills
            $versionDir = Split-Path $skillsDir  -Parent   # ...\<plugin>\<version>
            $pluginDir  = Split-Path $versionDir -Parent   # ...\<marketplace>\<plugin>
            $plugin     = Split-Path $pluginDir  -Leaf
            if ((Split-Path $skillsDir -Leaf) -eq 'skills') {
                $targets += [pscustomobject]@{
                    Scope   = "plugin-$plugin"
                    Name    = Split-Path $skillDir -Leaf
                    Path    = $skillDir
                    Plugin  = $plugin
                    Version = Split-Path $versionDir -Leaf
                }
            }
        }
    }

    $targets | Sort-Object Scope, Name
}

function Get-PluginVersionMap {
    <#
        Current <plugin> -> <version> map, as an ordered hashtable.

        A plugin upgrade rewrites the version segment of its cache path. The
        baselines survive that (they are keyed on plugin name, and findings are
        recorded relative to the skill root), which is exactly why an upgrade
        can otherwise slide past as CLEAN: same skill names, same relative
        paths, entirely new code. Tracking the version explicitly is what turns
        an upgrade into something the watch has to report.
    #>
    [CmdletBinding()]
    param([object[]]$Targets)

    if (-not $Targets) { $Targets = Get-SkillTargets }
    $map = [ordered]@{}
    foreach ($t in ($Targets | Where-Object { $_.Plugin } | Sort-Object Plugin)) {
        if (-not $map.Contains($t.Plugin)) { $map[$t.Plugin] = $t.Version }
    }
    $map
}

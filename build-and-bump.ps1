# build-and-bump.ps1 - release the Xero plugin
#
#   .\build-and-bump.ps1                  # 1.4.1 -> 1.4.2 (patch)
#   .\build-and-bump.ps1 -Bump minor      # 1.4.1 -> 1.5.0
#   .\build-and-bump.ps1 -Bump major      # 1.4.1 -> 2.0.0
#   .\build-and-bump.ps1 -Version 1.6.0   # set an exact version
#   .\build-and-bump.ps1 -SkipTests       # only if you really must
#   .\build-and-bump.ps1 -Yes             # don't ask (e.g. empty changelog)
#
# What it does, in order (it stops at the first problem, before changing anything
# you'd have to undo):
#   1. Checks: git is available, the new tag doesn't already exist here or on GitHub.
#   2. Build: regenerates docs/SETTINGS.md from the code.
#   3. Tests: lint and the unit tests, plus a syntax check of the UI file if Node is
#      installed. Needs your venv: pip install -e . pytest pyflakes
#   4. Bumps inventree_xero/version.py (the only place the version lives) and turns
#      "## Unreleased" in CHANGELOG.md into "## X.Y.Z - date".
#   5. Commits ALL current changes as "Release vX.Y.Z" and tags vX.Y.Z.
# It does NOT push. Push the commit AND the tag (PyCharm: tick "Push tags"),
# then update the server to the tag - the script prints exactly how.
#
# Why a tag: the server's plugins.txt pins inventree-xero to a release tag
# (...inventree-xero.git@vX.Y.Z). A new tag is a new install link, so pip really
# reinstalls; and only releases you tagged can ever reach the server - not a
# half-finished push to main. Important for a plugin that writes to your accounts.
#
# Works in Windows PowerShell 5.1 (PyCharm's terminal) and PowerShell 7.
# Keep this file plain ASCII: 5.1 reads BOM-less scripts as ANSI.

param(
    [ValidateSet('patch', 'minor', 'major')]
    [string]$Bump = 'patch',
    [string]$Version,
    [switch]$SkipTests,
    [switch]$Yes
)

# Native commands (git, python) report through exit codes; 'Stop' would turn their
# stderr chatter into exceptions in Windows PowerShell 5.1.
$ErrorActionPreference = 'Continue'
$repoUrl = 'https://github.com/SXSLYDA/inventree-xero.git'
$utf8 = New-Object System.Text.UTF8Encoding $false
$emDash = [string][char]0x2014

function Fail([string]$message) {
    Write-Host ""
    Write-Host "STOPPED: $message" -ForegroundColor Red
    Write-Host "Nothing was committed or tagged." -ForegroundColor Red
    Pop-Location
    exit 1
}

function Step([string]$title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

function Read-Text([string]$path) {
    return [System.IO.File]::ReadAllText($path, $utf8)
}

function Write-Text([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, $utf8)
}

function Find-Python {
    foreach ($candidate in @('python', 'python3')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            & $cmd.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $cmd.Source }
        }
    }
    return $null
}

$root = $PSScriptRoot
Push-Location $root

# ------------------------------------------------------------------ 1. checks
Step "Checking"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git isn't on PATH in this window." }
git rev-parse --is-inside-work-tree 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "this folder isn't a git repository." }

$versionPath = Join-Path $root 'inventree_xero\version.py'
$changelogPath = Join-Path $root 'CHANGELOG.md'
if (-not (Test-Path $versionPath)) { Fail "inventree_xero\version.py not found - run this from the inventree-xero repo root." }
if (-not (Test-Path $changelogPath)) { Fail "CHANGELOG.md not found." }

$versionText = Read-Text $versionPath
$match = [regex]::Match($versionText, 'PLUGIN_VERSION\s*=\s*[''"](\d+)\.(\d+)\.(\d+)[''"]')
if (-not $match.Success) { Fail "couldn't find PLUGIN_VERSION = `"X.Y.Z`" in version.py." }
$major = [int]$match.Groups[1].Value
$minor = [int]$match.Groups[2].Value
$patch = [int]$match.Groups[3].Value
$oldVersion = "$major.$minor.$patch"

if ($Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "-Version must look like 1.2.3" }
    $newVersion = $Version
} elseif ($Bump -eq 'major') {
    $newVersion = "$($major + 1).0.0"
} elseif ($Bump -eq 'minor') {
    $newVersion = "$major.$($minor + 1).0"
} else {
    $newVersion = "$major.$minor.$($patch + 1)"
}
$newParts = $newVersion.Split('.') | ForEach-Object { [int]$_ }
$isNewer = ($newParts[0] -gt $major) -or ($newParts[0] -eq $major -and $newParts[1] -gt $minor) -or
           ($newParts[0] -eq $major -and $newParts[1] -eq $minor -and $newParts[2] -gt $patch)
if (-not $isNewer) { Fail "$newVersion isn't newer than the current $oldVersion." }
$tag = "v$newVersion"

$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
Write-Host "Branch: $branch    Version: $oldVersion -> $newVersion    Tag: $tag"

if (git tag --list $tag) { Fail "tag $tag already exists here. Pick another version with -Version." }
$remoteTag = git ls-remote --tags origin "refs/tags/$tag" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Couldn't reach GitHub to check tags (offline?) - carrying on." -ForegroundColor Yellow
} elseif ($remoteTag) {
    Fail "tag $tag already exists on GitHub. Pull first, or pick another version with -Version."
}

# ------------------------------------------------------------------ 2. build
Step "Building"
$python = Find-Python
if (-not $python) {
    if ($SkipTests) {
        Write-Host "No Python 3.11+ found - can't regenerate docs/SETTINGS.md; leaving it as is." -ForegroundColor Yellow
    } else {
        Fail "no Python 3.11+ found. Activate your venv (PyCharm does this in its terminal), or use -SkipTests."
    }
} else {
    & $python tools/gen_settings_doc.py
    if ($LASTEXITCODE -ne 0) { Fail "couldn't regenerate docs/SETTINGS.md (see above)." }
}

# ------------------------------------------------------------------ 3. tests
Step "Testing"
if ($SkipTests) {
    Write-Host "Skipped (-SkipTests). CI on GitHub will still run every test on the tag." -ForegroundColor Yellow
} else {
    & $python -c "import pytest, pyflakes, django, cryptography, requests" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "the test tools aren't installed in this venv. Run once:  pip install -e . pytest pyflakes"
    }
    & $python -m pyflakes inventree_xero tests tools
    if ($LASTEXITCODE -ne 0) { Fail "lint found problems (above)." }
    & $python -m pytest -q tests/test_mapping.py tests/test_safety.py
    if ($LASTEXITCODE -ne 0) { Fail "unit tests failed (above)." }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "xero_ui_check.mjs"
        Copy-Item 'inventree_xero/static/xero_ui.js' $tmp -Force
        node --check $tmp
        $nodeExit = $LASTEXITCODE
        Remove-Item $tmp -ErrorAction SilentlyContinue
        if ($nodeExit -ne 0) { Fail "inventree_xero/static/xero_ui.js has a syntax error (above)." }
        Write-Host "UI file syntax OK"
    } else {
        Write-Host "Node not installed - skipping the UI syntax check (CI runs the UI tests)."
    }
    Write-Host "All local checks passed. (The full end-to-end suite runs in CI on GitHub.)" -ForegroundColor Green
}

# ------------------------------------------------------------------ 4. bump
Step "Bumping"
$changelog = Read-Text $changelogPath
$nl = if ($changelog.Contains("`r`n")) { "`r`n" } else { "`n" }
$heading = [regex]::Match($changelog, '(?m)^## Unreleased[ \t]*\r?$')
if (-not $heading.Success) {
    Fail "CHANGELOG.md has no '## Unreleased' heading. Add one at the top with this release's notes under it."
}
$bodyStart = $heading.Index + $heading.Length
$next = [regex]::Match($changelog.Substring($bodyStart), '(?m)^## ')
$bodyEnd = if ($next.Success) { $bodyStart + $next.Index } else { $changelog.Length }
$body = $changelog.Substring($bodyStart, $bodyEnd - $bodyStart).Trim()
if (-not $body) {
    Write-Host "There's nothing under '## Unreleased' in CHANGELOG.md." -ForegroundColor Yellow
    if (-not $Yes) {
        $answer = Read-Host "Release $tag without changelog notes? (y/N)"
        if ($answer -notmatch '^[Yy]') { Fail "add this release's notes under '## Unreleased' in CHANGELOG.md, then run again." }
    }
    $body = "- No notes."
}
$date = Get-Date -Format 'yyyy-MM-dd'
$released = "## Unreleased$nl$nl## $newVersion $emDash $date$nl$nl$body$nl$nl"
$changelog = $changelog.Substring(0, $heading.Index) + $released + $changelog.Substring($bodyEnd)
Write-Text $changelogPath $changelog

$versionText = [regex]::Replace($versionText, 'PLUGIN_VERSION\s*=\s*[''"]\d+\.\d+\.\d+[''"]', "PLUGIN_VERSION = `"$newVersion`"")
Write-Text $versionPath $versionText
Write-Host "Version bumped: $oldVersion -> $newVersion; CHANGELOG.md stamped '$newVersion $emDash $date'."

# ------------------------------------------------------------------ 5. commit and tag
Step "Committing"
git add -A
git status --short | Out-Host
git commit -q -m "Release $tag"
if ($LASTEXITCODE -ne 0) { Fail "git commit failed (above). Is your git user.name / user.email set?" }
git tag -a $tag -m "Release $tag"
if ($LASTEXITCODE -ne 0) { Fail "couldn't create tag $tag." }

Write-Host ""
Write-Host "Released $tag (committed and tagged, NOT pushed)." -ForegroundColor Green
Write-Host ""
Write-Host "1. Push the commit AND the tag:"
Write-Host "     PyCharm:  Git -> Push (Ctrl+Shift+K), tick 'Push tags' (bottom-left), then Push"
Write-Host "     Terminal: git push --follow-tags"
Write-Host "   Check the tag reached GitHub (prints a line ending in refs/tags/$tag):"
Write-Host "     git ls-remote --tags origin $tag"
Write-Host ""
Write-Host "2. Update the server. If GitHub deploys are switched on, that happens by itself"
Write-Host "   once CI passes on the tag. Otherwise, on the server:"
Write-Host "     sudo inventree-xero-update $tag"
Write-Host "   (which moves plugins.txt to:  inventree-xero @ git+$repoUrl@$tag )"
Write-Host ""
Write-Host "To undo before pushing:  git tag -d $tag; git reset --soft HEAD~1"
Pop-Location

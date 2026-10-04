# TokenTier uninstaller for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7).
# Thin wrapper: all logic lives in bin\tokentier. Removes exactly what the installer added
# (recorded in %USERPROFILE%\.tokentier\install-manifest.json); keeps your logs unless --purge.
#   .\uninstall.ps1 --dry-run    preview, change nothing
#   .\uninstall.ps1              remove everything TokenTier installed (asks once)
#   .\uninstall.ps1 --help       all options
# Blocked by the execution policy? Run uninstall.cmd instead (same arguments).
# This script downloads nothing and runs nothing except the Python found below.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cli = Join-Path $here 'bin\tokentier'

function Test-Python([string]$exe, [string[]]$pre) {
    # True if "$exe $pre" is a real Python 3.9+ (the Microsoft Store stub fails this check).
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $argv = @()
        if ($pre) { $argv += $pre }
        $argv += @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)')
        & $exe @argv *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $old
    }
}

function Find-Python {
    $candidates = @(
        @{ exe = 'py'; pre = @('-3') },
        @{ exe = 'python'; pre = @() },
        @{ exe = 'python3'; pre = @() }
    )
    foreach ($c in $candidates) {
        if (Test-Python $c.exe $c.pre) { return $c }
    }
    return $null
}

$py = Find-Python
if ($null -eq $py) {
    Write-Host 'TokenTier needs Python 3.9 or newer, but none was found (tried: py -3, python, python3).'
    Write-Host 'Install it from https://www.python.org/downloads/windows/ and run this script again.'
    exit 1
}
if (-not (Test-Path -LiteralPath $cli)) {
    Write-Host "TokenTier source tree is incomplete: $cli not found."
    exit 1
}

$argv = @()
if ($py.pre) { $argv += $py.pre }
$argv += @($cli, 'uninstall')
if ($args) { $argv += $args }
& $py.exe @argv
exit $LASTEXITCODE

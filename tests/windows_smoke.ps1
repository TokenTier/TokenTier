# TokenTier Windows smoke test (opt-in; run it by hand on a real Windows 10/11 machine).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows_smoke.ps1
#
# SAFE: everything happens in a throwaway profile dir. USERPROFILE/HOME point at a new temp
# folder whose name contains a space, TOKENTIER_HOME and CLAUDE_CONFIG_DIR are removed,
# TOKENTIER_NO_SERVICE_EXEC=1 is set and --no-service is used, so no scheduled task, registry
# entry or real ~\.claude file is touched. Your environment is restored at the end.
#
# Steps: seed settings.json (UTF-8 BOM + CRLF) and CLAUDE.md (CRLF) -> install --yes --no-service
# -> doctor -> fire a synthetic SessionStart through the hook command registered in settings.json
# (exec form: command + args, no shell) -> run the tokentier.cmd shim -> uninstall --yes -> check
# that settings.json and CLAUDE.md are byte-identical to the seeds and the kit files are gone.
# Exit code 0 = all checks passed. Please paste the output into an issue if anything fails.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$cli = Join-Path $repo 'bin\tokentier'
$failures = New-Object System.Collections.Generic.List[string]

function Check([bool]$ok, [string]$what) {
    if ($ok) { Write-Host "PASS  $what" } else { Write-Host "FAIL  $what"; $failures.Add($what) }
}

function Find-Python {
    foreach ($c in @(@{ exe = 'py'; pre = @('-3') }, @{ exe = 'python'; pre = @() }, @{ exe = 'python3'; pre = @() })) {
        if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $a = @()
            if ($c.pre) { $a += $c.pre }
            $a += @('-c', 'import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 9) else 1)')
            $out = & $c.exe @a 2> $null
            if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
        } catch {
        } finally {
            $ErrorActionPreference = $old
        }
    }
    return $null
}

function Join-Args([string[]]$argv) {
    # Windows command line for argv (each argument quoted; our arguments never end in a backslash)
    return (($argv | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join ' ')
}

function Invoke-Native([string]$exe, [string[]]$argv, [string]$stdin = $null, [string]$rawArgs = $null) {
    # Runs a native program; returns @{ code; out }. stdin (optional) is written as UTF-8.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    if ($rawArgs) { $psi.Arguments = $rawArgs } else { $psi.Arguments = Join-Args $argv }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    if ($stdin) {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($stdin)
        $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    }
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return @{ code = $p.ExitCode; out = $out }
}

$python = Find-Python
if ($null -eq $python) { Write-Host 'Python 3.9+ not found (py -3, python, python3).'; exit 1 }
Write-Host "python: $python"

$saved = @{}
foreach ($n in @('USERPROFILE', 'HOME', 'TOKENTIER_HOME', 'CLAUDE_CONFIG_DIR', 'TOKENTIER_NO_SERVICE_EXEC', 'CLAUDE_PROJECT_DIR')) {
    $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('tokentier-smoke-' + [guid]::NewGuid().ToString('N'))
$fakeHome = Join-Path $tmp 'home with space'
try {
    New-Item -ItemType Directory -Path (Join-Path $fakeHome '.claude') -Force | Out-Null
    $env:USERPROFILE = $fakeHome
    $env:HOME = $fakeHome
    $env:TOKENTIER_NO_SERVICE_EXEC = '1'
    Remove-Item Env:TOKENTIER_HOME -ErrorAction SilentlyContinue
    Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:CLAUDE_PROJECT_DIR -ErrorAction SilentlyContinue

    $claude = Join-Path $fakeHome '.claude'
    $settings = Join-Path $claude 'settings.json'
    $claudeMd = Join-Path $claude 'CLAUDE.md'
    $seedSettings = [byte[]](@(0xEF, 0xBB, 0xBF) + [System.Text.Encoding]::ASCII.GetBytes("{`r`n  `"model`": `"opus`",`r`n  `"hooks`": {}`r`n}`r`n"))
    $seedMd = [System.Text.Encoding]::ASCII.GetBytes("# My rules`r`n`r`nBe concise.`r`n")
    [System.IO.File]::WriteAllBytes($settings, $seedSettings)
    [System.IO.File]::WriteAllBytes($claudeMd, $seedMd)
    $hashS = (Get-FileHash -Algorithm SHA256 -LiteralPath $settings).Hash
    $hashM = (Get-FileHash -Algorithm SHA256 -LiteralPath $claudeMd).Hash

    # 1. install
    $r = Invoke-Native $python @($cli, 'install', '--yes', '--no-service')
    Write-Host $r.out
    Check ($r.code -eq 0) 'install --yes --no-service exits 0'
    $tt = Join-Path $fakeHome '.tokentier'
    Check (Test-Path -LiteralPath (Join-Path $tt 'app\hooks\tokentier_log.py')) 'app copied to %USERPROFILE%\.tokentier\app'
    Check (Test-Path -LiteralPath (Join-Path $tt 'app\bin\tokentier.cmd')) 'tokentier.cmd shim created'
    $raw = [System.IO.File]::ReadAllBytes($settings)
    Check ($raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) 'settings.json keeps its UTF-8 BOM'
    $text = [System.Text.Encoding]::UTF8.GetString($raw)
    Check (($text -replace "`r`n", '') -notmatch "`n") 'settings.json keeps CRLF line endings'

    # 2. doctor
    $r = Invoke-Native $python @((Join-Path $tt 'app\bin\tokentier'), 'doctor')
    Write-Host $r.out
    Check ($r.code -eq 0) 'doctor reports no FAIL'

    # 3. fire SessionStart through the registered hook (exec form)
    $json = $text.TrimStart([char]0xFEFF) | ConvertFrom-Json
    $h = $null
    foreach ($g in $json.hooks.SessionStart) {
        foreach ($x in $g.hooks) {
            if ($x.PSObject.Properties.Name -contains 'args' -and (($x.args -join ' ') -match 'tokentier_log\.py')) { $h = $x }
        }
    }
    Check ($null -ne $h) 'SessionStart hook registered in exec form (command + args)'
    if ($null -ne $h) {
        Check (Test-Path -LiteralPath $h.command) "hook interpreter exists: $($h.command)"
        $payload = '{"session_id":"smoke-1","cwd":"C:/smoke/proj","hook_event_name":"SessionStart","source":"startup"}'
        $r = Invoke-Native $h.command ([string[]]$h.args) $payload
        Check ($r.code -eq 0 -and $r.out.Trim() -eq '') 'hook exits 0 and prints nothing'
        $logs = @(Get-ChildItem -LiteralPath (Join-Path $tt 'logs') -Filter '*.jsonl' -ErrorAction SilentlyContinue)
        $found = $false
        foreach ($f in $logs) {
            $b = [System.IO.File]::ReadAllBytes($f.FullName)
            $s = [System.Text.Encoding]::UTF8.GetString($b)
            if ($s -match '"session_start"' -and $s -match 'smoke-1') { $found = $true }
            Check (-not ($s -match "`r")) "log $($f.Name) uses LF line endings (no CRLF)"
        }
        Check $found 'session_start event appended to logs\YYYY-MM-DD.jsonl'
    }

    # 4. the cmd shim
    # cmd /s /c ""C:\path with space\tokentier.cmd" version": the outer quotes are stripped by cmd
    $shim = Join-Path $tt 'app\bin\tokentier.cmd'
    $r = Invoke-Native $env:ComSpec @() $null ('/d /s /c ""' + $shim + '" version"')
    Check ($r.code -eq 0 -and $r.out -match 'tokentier') 'tokentier.cmd version works'

    # 5. uninstall and compare
    $r = Invoke-Native $python @($cli, 'uninstall', '--yes')
    Write-Host $r.out
    Check ($r.code -eq 0) 'uninstall --yes exits 0'
    Check ((Get-FileHash -Algorithm SHA256 -LiteralPath $settings).Hash -eq $hashS) 'settings.json restored byte-for-byte'
    Check ((Get-FileHash -Algorithm SHA256 -LiteralPath $claudeMd).Hash -eq $hashM) 'CLAUDE.md restored byte-for-byte'
    Check (-not (Test-Path -LiteralPath (Join-Path $claude 'agents\fast-worker.md'))) 'agents removed'
    Check (-not (Test-Path -LiteralPath (Join-Path $tt 'app'))) 'app dir removed'
} finally {
    foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($failures.Count -eq 0) {
    Write-Host 'Windows smoke test: ALL CHECKS PASSED'
    exit 0
}
Write-Host ("Windows smoke test: {0} check(s) FAILED:" -f $failures.Count)
foreach ($f in $failures) { Write-Host "  - $f" }
exit 1

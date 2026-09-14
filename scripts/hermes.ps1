# Launch the director without changing machine-wide active-profile settings.
$ErrorActionPreference = 'Stop'
$HermesRuntime = Join-Path $env:LOCALAPPDATA 'hermes'
$HermesExecutable = Join-Path $HermesRuntime 'bin/hermes.exe'
if (-not (Test-Path -LiteralPath $HermesExecutable)) {
    throw 'Hermes is not installed. See README.md.'
}
$env:HERMES_HOME = $HermesRuntime
$env:HERMES_GIT_BASH_PATH = 'C:\Program Files\Git\bin\bash.exe'
$env:PYTHONUTF8 = '1'
if ($args.Count -eq 0) {
    & $HermesExecutable -p claw --cli
} else {
    & $HermesExecutable -p claw @args
}
exit $LASTEXITCODE

param(
    [switch]$Demo,
    [string]$ConnectScript = ''
)

$ErrorActionPreference = 'Stop'
$app = Join-Path $PSScriptRoot 'pods_local.py'
if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
    throw "No se encontro pods_local.py junto a este script."
}

$arguments = @()
if ($Demo) { $arguments += '--demo' }
if ($ConnectScript) { $arguments += @('--connect-script', $ConnectScript) }

Set-Location -LiteralPath $PSScriptRoot
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    & $launcher.Source -3 $app @arguments
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw 'Instala Python 3.9+ y agregalo al PATH.' }
    & $python.Source $app @arguments
}
exit $LASTEXITCODE

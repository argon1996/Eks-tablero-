param(
    [switch]$NoDesktop,
    [switch]$NoTaskbar
)

$ErrorActionPreference = 'Stop'
$sourceRoot = (Get-Item -LiteralPath $PSScriptRoot).FullName
$sourceLauncher = Join-Path $sourceRoot 'launch-console.ps1'
$sourcePackage = Join-Path $sourceRoot 'eks_console'

if (-not (Test-Path -LiteralPath $sourceLauncher -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $sourcePackage '__init__.py') -PathType Leaf)) {
    throw 'La carpeta está incompleta. Debe incluir launch-console.ps1 y la carpeta eks_console.'
}
if (-not $env:LOCALAPPDATA) {
    throw 'Windows no informó la carpeta LOCALAPPDATA del usuario.'
}

$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\Bancolombia EKS Console'
$installRoot = [IO.Path]::GetFullPath($installRoot)
if ($installRoot.Contains('"')) { throw 'La ruta de instalación contiene comillas no admitidas.' }

# Copia solo los archivos propios de la aplicación. No copia .git, cachés ni scripts
# de conexión del usuario que podrían contener información sensible.
if ($sourceRoot -ne $installRoot) {
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    foreach ($name in @('pods_local.py', 'launch-console.ps1', 'install-shortcut.ps1', 'README.md')) {
        $source = Join-Path $sourceRoot $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $installRoot $name) -Force
        }
    }
    $targetPackage = Join-Path $installRoot 'eks_console'
    $targetWeb = Join-Path $targetPackage 'web'
    New-Item -ItemType Directory -Path $targetWeb -Force | Out-Null
    Get-ChildItem -LiteralPath $sourcePackage -File -Filter '*.py' |
        Copy-Item -Destination $targetPackage -Force
    Get-ChildItem -LiteralPath (Join-Path $sourcePackage 'web') -File |
        Copy-Item -Destination $targetWeb -Force
}

$launcher = Join-Path $installRoot 'launch-console.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw 'No fue posible copiar el lanzador a la carpeta de instalación.'
}
$appEntry = Join-Path $installRoot 'pods_local.py'
if (-not (Test-Path -LiteralPath $appEntry -PathType Leaf)) {
    throw 'No fue posible copiar la aplicación a la carpeta de instalación.'
}

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw 'Instala Python 3.9 o superior y agrégalo al PATH.' }
    & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
}
if ($LASTEXITCODE -ne 0) { throw 'EKS Console requiere Python 3.9 o superior.' }

$shell = New-Object -ComObject WScript.Shell
$pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
$pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pyw) {
    $shortcutTarget = $pyw.Source
    $shortcutArguments = '-3 "{0}"' -f $appEntry
} elseif ($pythonw) {
    $shortcutTarget = $pythonw.Source
    $shortcutArguments = '"{0}"' -f $appEntry
} else {
    $shortcutTarget = $python.Source
    $shortcutArguments = if ($python.Name -ieq 'py.exe') { '-3 "{0}"' -f $appEntry } else { '"{0}"' -f $appEntry }
}
$shortcutName = 'Bancolombia EKS Console.lnk'

function New-EksShortcut([string]$folder) {
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    $shortcutPath = Join-Path $folder $shortcutName
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $shortcutTarget
    $shortcut.Arguments = $shortcutArguments
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.Description = 'Bancolombia EKS Console · operación local de solo lectura'
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Host "Acceso creado: $shortcutPath"
}

$startMenu = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
New-EksShortcut $startMenu
if (-not $NoDesktop) {
    New-EksShortcut ([Environment]::GetFolderPath('DesktopDirectory'))
}

# Windows puede reconstruir la lista de anclados según sus políticas. Se crea el
# acceso en la ubicación estándar y se mantiene también en Inicio como respaldo.
if (-not $NoTaskbar) {
    $taskbar = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
    New-EksShortcut $taskbar
}

Write-Host ''
Write-Host "EKS Console instalada en: $installRoot"
Write-Host 'Puedes abrirla desde Inicio, el Escritorio o la barra de tareas.'
Write-Host 'Si Windows no muestra el icono anclado de inmediato, búscalo en Inicio, haz clic derecho y elige Anclar a la barra de tareas.'

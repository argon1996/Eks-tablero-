param([switch]$NoDesktop)

$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'launch-console.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw 'Ejecuta este instalador desde la carpeta completa del proyecto.'
}
if ($launcher.Contains('"')) { throw 'La ruta del proyecto contiene comillas no admitidas.' }

$shell = New-Object -ComObject WScript.Shell
$powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
$powershellPath = if ($powershell) { $powershell.Source } else { (Get-Process -Id $PID).Path }
$name = 'Bancolombia EKS Console.lnk'
$folders = @(
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
if (-not $NoDesktop) { $folders += [Environment]::GetFolderPath('DesktopDirectory') }

# En algunas instalaciones, Links se muestra como Favoritos en el Explorador.
$links = Join-Path $env:USERPROFILE 'Links'
if (Test-Path -LiteralPath $links -PathType Container) { $folders += $links }

foreach ($folder in ($folders | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    $shortcut = $shell.CreateShortcut((Join-Path $folder $name))
    $shortcut.TargetPath = $powershellPath
    $shortcut.Arguments = '-NoProfile -File "{0}"' -f $launcher
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'Abrir tablero local EKS'
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $shortcut.Save()
    Write-Host "Acceso creado: $(Join-Path $folder $name)"
}

Write-Host 'Puedes anclar el acceso de Inicio a la barra de tareas desde Windows.'

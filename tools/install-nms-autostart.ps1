[CmdletBinding()]
param(
    [string]$StartupDir = [Environment]::GetFolderPath('Startup'),
    [switch]$Remove,
    [switch]$NoStart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$supervisor = Join-Path $PSScriptRoot 'nms-autostart.ps1'
$repoRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $StartupDir 'NMS-Kopegtel.lnk'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

if ($Remove) {
    if (Test-Path -LiteralPath $launcher) {
        if ($DryRun) {
            Write-Host "DryRun: hapus shortcut $launcher"
            exit 0
        }
        Remove-Item -LiteralPath $launcher -Force
        Write-Host "Shortcut autostart dihapus: $launcher"
    } else {
        Write-Host "Shortcut autostart tidak ada: $launcher"
    }
    Write-Host 'Proses supervisor/NMS yang sedang berjalan tidak dihentikan oleh -Remove.'
    exit 0
}

if (-not (Test-Path -LiteralPath $supervisor)) {
    throw "Script supervisor tidak ditemukan: $supervisor"
}
if (-not (Test-Path -LiteralPath $powershell)) {
    throw "Windows PowerShell tidak ditemukan: $powershell"
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $supervisor

if ($DryRun) {
    Write-Host "DryRun: buat shortcut $launcher"
    Write-Host "DryRun: target $powershell"
    Write-Host "DryRun: argumen $arguments"
    Write-Host "DryRun: working directory $repoRoot"
    exit 0
}

if (-not (Test-Path -LiteralPath $StartupDir)) {
    New-Item -ItemType Directory -Path $StartupDir -Force | Out-Null
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($launcher)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $repoRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'NMS Kopegtel autostart'
$shortcut.Save()

Write-Host "Shortcut autostart dibuat: $launcher"
Write-Host "Target: $powershell $arguments"
Write-Host "Working directory: $repoRoot"

if ($NoStart) {
    Write-Host 'Supervisor tidak dijalankan sekarang (-NoStart).'
    exit 0
}

$logDirectory = Join-Path $repoRoot 'logs'
if (-not (Test-Path -LiteralPath $logDirectory)) {
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
}
$logPath = Join-Path $logDirectory 'nms-autostart.log'

$process = Start-Process -FilePath $powershell -ArgumentList $arguments `
    -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru
Write-Host "Supervisor dijalankan (PID $($process.Id)); log: $logPath"
exit 0

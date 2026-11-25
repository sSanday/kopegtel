[CmdletBinding()]
param(
    [string]$AuthKey = $env:TAILSCALE_AUTH_KEY,
    [string]$Subnet = '192.168.30.0/24',
    [string]$Hostname = 'kopegtel-office',
    [string]$Target = '192.168.30.1',
    [switch]$Reset,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-TailscalePath {
    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe')
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Tailscale\tailscale.exe')
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    $command = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Install-Tailscale {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'Menginstall Tailscale via winget...'
        & $winget.Source install --id Tailscale.Tailscale -e --silent --accept-package-agreements --accept-source-agreements
        $path = Get-TailscalePath
        if ($path) { return $path }
        Write-Host 'winget belum berhasil; mencoba installer resmi...'
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $installer = Join-Path $env:TEMP 'tailscale-setup-latest.exe'
    Write-Host 'Mengunduh installer Tailscale...'
    (New-Object System.Net.WebClient).DownloadFile(
        'https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe',
        $installer
    )
    Write-Host 'Menjalankan installer (senyap)...'
    $process = Start-Process -FilePath $installer -ArgumentList '/quiet', '/norestart' -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Installer Tailscale gagal (exit $($process.ExitCode)). Jalankan installer manual dari https://tailscale.com/download/windows"
    }
    return Get-TailscalePath
}

function Start-TailscaleService {
    $service = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
    if (-not $service) {
        throw 'Service Tailscale tidak ditemukan setelah instalasi.'
    }
    if ($service.Status -ne 'Running') {
        Start-Service -Name Tailscale
    }
    for ($i = 0; $i -lt 30; $i++) {
        $service = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
        if ($service -and $service.Status -eq 'Running') { return }
        Start-Sleep -Seconds 1
    }
    throw 'Service Tailscale tidak berjalan setelah 30 detik.'
}

function Invoke-TailscaleUp {
    param(
        [string]$TailscalePath,
        [string]$AuthKey,
        [string]$Subnet,
        [string]$Hostname,
        [switch]$Reset,
        [switch]$DryRun
    )

    $tsArgs = @(
        'up',
        "--advertise-routes=$Subnet",
        '--accept-routes',
        "--hostname=$Hostname"
    )
    if ($Reset) { $tsArgs += '--reset' }
    if ($AuthKey) { $tsArgs += "--authkey=$AuthKey" }

    $displayArgs = $tsArgs | ForEach-Object {
        if ($_ -like '--authkey=*') { '--authkey=<REDACTED>' } else { $_ }
    }
    Write-Host ("Perintah: tailscale " + ($displayArgs -join ' '))

    if ($DryRun) { return }
    if (-not $TailscalePath) { throw 'tailscale.exe tidak ditemukan.' }

    & $TailscalePath @tsArgs
    if ($LASTEXITCODE -ne 0) {
        throw "tailscale up gagal (exit $LASTEXITCODE). Jika node sudah pernah login dengan konfigurasi lain, jalankan ulang dengan -Reset."
    }
}

function Test-OfficeTarget {
    param([string]$Target)
    Write-Host "Menguji ping ke $Target..."
    $ok = Test-Connection -ComputerName $Target -Count 2 -Quiet -ErrorAction SilentlyContinue
    if ($ok) {
        Write-Host "Ping ke ${Target}: BERHASIL"
    } else {
        Write-Host "Ping ke ${Target}: GAGAL. Pastikan PC ini berada di jaringan kantor dan bisa mencapai $Target."
    }
}

$isAdmin = Test-IsAdmin
$tailscale = Get-TailscalePath

if ($DryRun) {
    Write-Host '== Dry-run setup Tailscale subnet router =='
    Write-Host "Subnet    : $Subnet"
    Write-Host "Hostname  : $Hostname"
    Write-Host "Target    : $Target"
    Write-Host "Admin     : $isAdmin"
    Write-Host "Tailscale : $(if ($tailscale) { $tailscale } else { 'TIDAK terdeteksi' })"
    if (-not $tailscale) {
        Write-Host 'Aksi      : install Tailscale (winget atau installer resmi)'
    }
    Write-Host 'Aksi      : start service Tailscale'
    Invoke-TailscaleUp -TailscalePath $tailscale -AuthKey $AuthKey -Subnet $Subnet -Hostname $Hostname -Reset:$Reset -DryRun
    Write-Host 'Aksi      : tailscale status'
    Write-Host 'Aksi      : tailscale ip -4'
    Write-Host "Aksi      : ping $Target"
    exit 0
}

if (-not $isAdmin) {
    Write-Host 'Jalankan PowerShell sebagai Administrator.'
    exit 1
}

if (-not $tailscale) {
    $tailscale = Install-Tailscale
    if (-not $tailscale) {
        Write-Host 'Tailscale tidak ditemukan setelah instalasi. Jalankan installer manual dari https://tailscale.com/download/windows'
        exit 1
    }
}

Start-TailscaleService
Invoke-TailscaleUp -TailscalePath $tailscale -AuthKey $AuthKey -Subnet $Subnet -Hostname $Hostname -Reset:$Reset

Write-Host '== Status Tailscale =='
& $tailscale status
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Status belum tersedia; login mungkin belum selesai.'
}
Write-Host '== IP Tailscale =='
& $tailscale ip -4

Test-OfficeTarget -Target $Target

if ($env:TAILSCALE_AUTH_KEY) {
    Remove-Item Env:TAILSCALE_AUTH_KEY -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Langkah berikutnya:'
Write-Host '1. Buka https://login.tailscale.com/admin/machines'
Write-Host "2. Pilih mesin '$Hostname' -> Edit route settings -> approve $Subnet"
Write-Host '3. Di laptop: tailscale up --accept-routes'
Write-Host "4. Uji: ping $Target dan python tools/pilot_connectivity.py --device 6"

exit 0

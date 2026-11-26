[CmdletBinding()]
param(
    [string]$AuthKeyFile,
    [string]$Subnet = '192.168.30.0/24',
    [string]$Hostname = 'kopegtel-office',
    [string]$Target = '192.168.30.1',
    [string]$TailscalePath,
    [switch]$Reset,
    [switch]$DryRun,
    # Hanya untuk smoke test otomatis. Jangan dipakai di PC kantor.
    [switch]$SkipAdminCheck,
    [switch]$SkipForwardingCheck,
    [switch]$SkipServiceCheck,
    [switch]$SkipTargetCheck
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tailscale-setup-common.ps1')

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

function Show-ForwardingInstructions {
    Write-Host 'IP forwarding Windows belum aktif.'
    Write-Host 'Route boleh diiklankan, tetapi paket tidak akan diteruskan ke 192.168.30.1.'
    Write-Host 'Jalankan PowerShell sebagai Administrator:'
    Write-Host "  Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name IPEnableRouter -Value 1"
    Write-Host '  Restart-Computer'
    Write-Host 'Setelah reboot, jalankan script ini lagi.'
}

$isAdmin = Test-IsAdmin
$tailscale = if ($TailscalePath) { $TailscalePath } else { Get-TailscalePath }
$forwarding = Test-IPForwardingEnabled
$hasKeyInput = [bool]$AuthKeyFile

if ($DryRun) {
    Write-Host '== Dry-run setup Tailscale subnet router =='
    Write-Host "Subnet    : $Subnet"
    Write-Host "Hostname  : $Hostname"
    Write-Host "Target    : $Target"
    Write-Host "Admin     : $isAdmin"
    Write-Host "Tailscale : $(if ($tailscale) { $tailscale } else { 'TIDAK terdeteksi' })"
    Write-Host "IP forward: $(if ($forwarding) { 'AKTIF' } else { 'TIDAK AKTIF' })"
    if (-not $tailscale) {
        Write-Host 'Aksi      : install Tailscale (winget atau installer resmi)'
    }
    Write-Host 'Aksi      : start service Tailscale'
    if (-not $forwarding) {
        Write-Host 'Aksi      : aktifkan IPEnableRouter=1 lalu reboot'
    }

    $upAuthFile = if ($hasKeyInput) { '<auth-key-file>' } else { '' }
    $upArgs = New-TailscaleUpArgs -AuthKeyFile $upAuthFile -Subnet $Subnet -Hostname $Hostname -Reset:$Reset
    Write-Host ("Perintah: " + (Format-TailscaleCommand -Arguments $upArgs))
    $setArgs = New-TailscaleSetArgs -Subnet $Subnet -Hostname $Hostname
    Write-Host ("Perintah: " + (Format-TailscaleCommand -Arguments $setArgs))

    Write-Host 'Aksi      : tailscale status --json (tentukan Running/NeedsLogin/Stopped)'
    Write-Host 'Aksi      : tailscale status'
    Write-Host 'Aksi      : tailscale ip -4'
    Write-Host "Aksi      : ping $Target"
    exit 0
}

if (-not $isAdmin -and -not $SkipAdminCheck) {
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

if (-not $SkipServiceCheck) {
    Start-TailscaleService
}

if (-not $forwarding -and -not $SkipForwardingCheck) {
    Show-ForwardingInstructions
    exit 1
}

$tempKeyFile = $null
try {
    $backendState = Get-TailscaleBackendState -Path $tailscale
    Write-Host "Status backend Tailscale: $(if ($backendState) { $backendState } else { 'tidak terbaca' })"

    $shouldRunUp = Test-ShouldRunTailscaleUp -BackendState $backendState -Reset:$Reset
    $needsKey = Test-TailscaleNeedsAuthKey -BackendState $backendState
    $key = $null

    if ($shouldRunUp -and $needsKey) {
        if ($AuthKeyFile) {
            if (-not (Test-Path -LiteralPath $AuthKeyFile)) {
                throw "File auth key tidak ditemukan: $AuthKeyFile"
            }
            $key = (Get-Content -LiteralPath $AuthKeyFile -Raw).Trim()
            if (-not $key) {
                throw "File auth key kosong: $AuthKeyFile"
            }
        } else {
            Write-Host 'Belum login ke Tailscale.'
            $key = Read-AuthKeySecure
            if (-not $key) {
                throw 'Auth key kosong. Buat auth key di https://login.tailscale.com/admin/settings/keys lalu jalankan ulang.'
            }
        }
    } elseif ($AuthKeyFile) {
        Write-Host 'Node sudah terautentikasi; auth key diabaikan.'
    }

    if ($key) {
        $tempKeyFile = New-ProtectedAuthKeyFile -AuthKey $key
    }

    if ($shouldRunUp) {
        $upAuthFile = if ($key) { $tempKeyFile } else { '' }
        $upArgs = New-TailscaleUpArgs -AuthKeyFile $upAuthFile -Subnet $Subnet -Hostname $Hostname -Reset:$Reset
        Invoke-TailscaleCommand -TailscalePath $tailscale -Arguments $upArgs -Redact $key
    } else {
        Write-Host 'Node Running; melewati tailscale up.'
    }

    $setArgs = New-TailscaleSetArgs -Subnet $Subnet -Hostname $Hostname
    Invoke-TailscaleCommand -TailscalePath $tailscale -Arguments $setArgs

    Write-Host '== Status Tailscale =='
    $statusResult = Invoke-NativeCapture -Path $tailscale -Arguments @('status')
    foreach ($line in $statusResult.Output) { Write-Host ([string]$line) }
    if ($statusResult.ExitCode -ne 0) {
        Write-Host 'Status belum tersedia; login mungkin belum selesai.'
    }
    Write-Host '== IP Tailscale =='
    $ipResult = Invoke-NativeCapture -Path $tailscale -Arguments @('ip', '-4')
    foreach ($line in $ipResult.Output) { Write-Host ([string]$line) }

    if (-not $SkipTargetCheck) {
        Test-OfficeTarget -Target $Target
    }

    Write-Host ''
    Write-Host 'Langkah berikutnya:'
    Write-Host '1. Buka https://login.tailscale.com/admin/machines'
    Write-Host "2. Pilih mesin '$Hostname' -> Edit route settings -> approve $Subnet"
    Write-Host '3. Di laptop: tailscale set --accept-routes'
    Write-Host "4. Uji: ping $Target dan python tools/pilot_connectivity.py --device 6"

    exit 0
} finally {
    if ($tempKeyFile) {
        try {
            Remove-Item -LiteralPath $tempKeyFile -Force -ErrorAction Stop
        } catch {
            Write-Warning "Gagal menghapus file auth key sementara: $tempKeyFile"
        }
    }
}

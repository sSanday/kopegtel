$ErrorActionPreference = 'Stop'

$supervisor = Join-Path $PSScriptRoot 'nms-autostart.ps1'
$installer = Join-Path $PSScriptRoot 'install-nms-autostart.ps1'
if (-not (Test-Path -LiteralPath $supervisor)) { throw "Script belum ada: $supervisor" }
if (-not (Test-Path -LiteralPath $installer)) { throw "Installer belum ada: $installer" }

. $supervisor

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

$tempDir = Join-Path $env:TEMP ("nms-autostart-test-{0}" -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
$log = Join-Path $tempDir 'supervisor.log'
$mutex = "Local\NMS-Autostart-Test-$([guid]::NewGuid().ToString('N'))"
$listener = $null

try {
    # 1. NMS sudah sehat: supervisor tidak menyalakan instance kedua.
    $port = Get-FreePort
    $result = Invoke-Supervisor -Once -DryRun -Port $port -Target '127.0.0.1' `
        -LogPath $log -MutexName $mutex `
        -HealthProbe { $true } -NetworkProbe { $false }
    if ($result -ne 0) { throw "Health=true harus exit 0, dapat $result" }
    if ((Get-Content -LiteralPath $log -Raw) -notmatch 'sudah sehat') {
        throw "Log tidak menyatakan NMS sudah sehat"
    }

    # 2. Jaringan kantor belum terjangkau: jangan start (mencegah offline palsu).
    Remove-Item -LiteralPath $log -Force
    $result = Invoke-Supervisor -Once -DryRun -Port $port -Target '127.0.0.1' `
        -LogPath $log -MutexName $mutex `
        -HealthProbe { $false } -NetworkProbe { $false }
    if ($result -ne 0) { throw "Network=false harus exit 0, dapat $result" }
    $text = Get-Content -LiteralPath $log -Raw
    if ($text -notmatch 'belum terjangkau') { throw "Log tidak menyatakan target belum terjangkau" }
    if ($text -match 'DryRun:') { throw "Supervisor tetap mencoba start saat jaringan salah" }

    # 3. Jaringan OK dan NMS mati: dry-run hanya melaporkan perintah start.
    Remove-Item -LiteralPath $log -Force
    $result = Invoke-Supervisor -Once -DryRun -Port $port -Target '127.0.0.1' `
        -LogPath $log -MutexName $mutex `
        -HealthProbe { $false } -NetworkProbe { $true }
    if ($result -ne 0) { throw "DryRun start harus exit 0, dapat $result" }
    $text = Get-Content -LiteralPath $log -Raw
    if ($text -notmatch 'DryRun:') { throw "DryRun tidak melaporkan perintah start" }
    if ($text -notmatch 'serve\.py') { throw "DryRun tidak menyebut serve.py" }

    # 4. Port dipakai proses lain yang tidak sehat: jangan start, exit 1.
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $busyPort = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    Remove-Item -LiteralPath $log -Force
    $result = Invoke-Supervisor -Once -DryRun -Port $busyPort -Target '127.0.0.1' `
        -LogPath $log -MutexName $mutex `
        -HealthProbe { $false } -NetworkProbe { $true }
    if ($result -ne 1) { throw "Port sibuk harus exit 1, dapat $result" }
    if ((Get-Content -LiteralPath $log -Raw) -notmatch 'dipakai') {
        throw "Log tidak menyatakan port dipakai proses lain"
    }
    $listener.Stop()
    $listener = $null

    # 5. Deteksi port harus akurat.
    $freePort = Get-FreePort
    if (Test-PortInUse -Port $freePort) { throw "Port $freePort seharusnya bebas" }
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $openPort = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    if (-not (Test-PortInUse -Port $openPort)) { throw "Port $openPort seharusnya terdeteksi terbuka" }
    $listener.Stop()
    $listener = $null

    # 6. Script harus bisa dijalankan sebagai proses -File (cara shortcut Startup).
    $childLog = Join-Path $tempDir 'child-supervisor.log'
    $childPort = Get-FreePort
    $childMutex = "Local\NMS-Autostart-Child-$([guid]::NewGuid().ToString('N'))"
    $childShell = (Get-Process -Id $PID).Path
    & $childShell -NoProfile -ExecutionPolicy Bypass -File $supervisor -Once -DryRun `
        -Port $childPort -Target '127.0.0.1' -LogPath $childLog -MutexName $childMutex 6>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Supervisor -File gagal (exit $LASTEXITCODE)" }
    if (-not (Test-Path -LiteralPath $childLog)) { throw "Supervisor -File tidak menulis log" }
    if ((Get-Content -LiteralPath $childLog -Raw) -notmatch 'DryRun:') {
        throw "Supervisor -File tidak mencapai DryRun"
    }

    # 7. Installer dry-run tidak boleh menulis shortcut.
    $dry = & $installer -StartupDir $tempDir -NoStart -DryRun 6>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "Installer dry-run gagal (exit $LASTEXITCODE)`n$dry" }
    $launcher = Join-Path $tempDir 'NMS-Kopegtel.lnk'
    if (Test-Path -LiteralPath $launcher) { throw "Installer dry-run menulis shortcut" }

    # 8. Installer membuat shortcut yang menunjuk wrapper; -Remove menghapusnya.
    & $installer -StartupDir $tempDir -NoStart | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Installer gagal (exit $LASTEXITCODE)" }
    if (-not (Test-Path -LiteralPath $launcher)) { throw "Shortcut autostart tidak dibuat" }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($launcher)
    if ($shortcut.TargetPath -notmatch 'powershell\.exe$') { throw "Target shortcut salah: $($shortcut.TargetPath)" }
    if ($shortcut.Arguments -notmatch [regex]::Escape($supervisor)) { throw "Shortcut tidak menunjuk supervisor" }
    if ($shortcut.WorkingDirectory -ne (Split-Path -Parent $PSScriptRoot)) { throw "WorkingDirectory shortcut salah: $($shortcut.WorkingDirectory)" }

    & $installer -StartupDir $tempDir -Remove | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Installer -Remove gagal (exit $LASTEXITCODE)" }
    if (Test-Path -LiteralPath $launcher) { throw "Shortcut autostart tidak terhapus" }

    Write-Host 'OK: autostart smoke test valid'
} finally {
    if ($listener) { $listener.Stop() }
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

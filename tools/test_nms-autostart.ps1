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
$savedEnvPort = $env:PORT
$savedEnvDb = $env:NMS_DB_PATH
$savedEnvBackup = $env:DB_BACKUP_ENABLED
$realProcess = $null

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

    # 6. Mode quiet tidak menulis log rutin, tetapi mengembalikan state.
    $quietLog = Join-Path $tempDir 'quiet.log'
    Remove-Item -LiteralPath $quietLog -ErrorAction SilentlyContinue
    $cycle = Invoke-SupervisorCycle -Port $freePort -Target '127.0.0.1' `
        -RepoRoot (Split-Path -Parent $PSScriptRoot) -LogPath $quietLog -Quiet `
        -HealthProbe { $true } -NetworkProbe { $false }
    if ($cycle.State -ne 'healthy') { throw "State quiet harus healthy, dapat $($cycle.State)" }
    if (Test-Path -LiteralPath $quietLog) { throw "Mode quiet masih menulis log rutin" }
    $null = Invoke-SupervisorCycle -Port $freePort -Target '127.0.0.1' `
        -RepoRoot (Split-Path -Parent $PSScriptRoot) -LogPath $quietLog `
        -HealthProbe { $true } -NetworkProbe { $false }
    if ((Get-Content -LiteralPath $quietLog -Raw) -notmatch 'sudah sehat') {
        throw "Mode non-quiet tidak menulis status sehat"
    }

    # 7. Port mengikuti PORT/.env, bukan hard-code 5000.
    $dotEnvPort = Get-FreePort
    $envPortValue = Get-FreePort
    $portRepo = Join-Path $tempDir 'port-repo'
    New-Item -ItemType Directory -Path $portRepo | Out-Null
    Set-Content -LiteralPath (Join-Path $portRepo '.env') -Value "PORT=$dotEnvPort" -Encoding ASCII
    Remove-Item Env:PORT -ErrorAction SilentlyContinue
    $resolved = Get-ConfiguredPort -RepoRoot $portRepo -Override 0
    if ($resolved -ne $dotEnvPort) { throw "Get-ConfiguredPort .env salah: $resolved, expected $dotEnvPort" }
    $override = Get-ConfiguredPort -RepoRoot $portRepo -Override 5555
    if ($override -ne 5555) { throw "Override port salah: $override" }
    $env:PORT = [string]$envPortValue
    $fromEnv = Get-ConfiguredPort -RepoRoot $portRepo -Override 0
    if ($fromEnv -ne $envPortValue) { throw "Env PORT tidak diprioritaskan: $fromEnv" }
    Remove-Item Env:PORT -ErrorAction SilentlyContinue
    $emptyRepo = Join-Path $tempDir 'empty-repo'
    New-Item -ItemType Directory -Path $emptyRepo | Out-Null
    $defaultPort = Get-ConfiguredPort -RepoRoot $emptyRepo -Override 0
    if ($defaultPort -ne 5500) { throw "Default port harus 5500, dapat $defaultPort" }
    $portDryLog = Join-Path $tempDir 'port-dryrun.log'
    $result = Invoke-Supervisor -Once -DryRun -Port 0 -RepoRoot $portRepo -Target '127.0.0.1' `
        -LogPath $portDryLog -MutexName $mutex `
        -HealthProbe { $false } -NetworkProbe { $true }
    if ($result -ne 0) { throw "DryRun port .env harus exit 0, dapat $result" }
    if ((Get-Content -LiteralPath $portDryLog -Raw) -notmatch "--port $dotEnvPort") {
        throw "DryRun tidak memakai PORT dari .env"
    }

    # 8. Script harus bisa dijalankan sebagai proses -File di kedua shell.
    $shells = @()
    $winPs = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $winPs) { $shells += $winPs }
    $pwshCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($pwshCommand) { $shells += $pwshCommand.Source }
    if ($shells.Count -eq 0) { $shells += (Get-Process -Id $PID).Path }
    $shells = @($shells | Select-Object -Unique)
    foreach ($shellPath in $shells) {
        $childLog = Join-Path $tempDir ("child-{0}.log" -f [IO.Path]::GetFileNameWithoutExtension($shellPath))
        $childPort = Get-FreePort
        $childMutex = "Local\NMS-Autostart-Child-$([guid]::NewGuid().ToString('N'))"
        & $shellPath -NoProfile -ExecutionPolicy Bypass -File $supervisor -Once -DryRun `
            -Port $childPort -Target '127.0.0.1' -LogPath $childLog -MutexName $childMutex 6>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Supervisor -File gagal di $shellPath (exit $LASTEXITCODE)" }
        if (-not (Test-Path -LiteralPath $childLog)) { throw "Supervisor -File tidak menulis log di $shellPath" }
        if ((Get-Content -LiteralPath $childLog -Raw) -notmatch 'DryRun:') {
            throw "Supervisor -File tidak mencapai DryRun di $shellPath"
        }
    }

    # 9. -DryRun tanpa -Once harus ditolak dan tidak boleh menyalakan NMS.
    $guardLog = Join-Path $tempDir 'dryrun-guard.log'
    $guardPort = Get-FreePort
    $guardMutex = "Local\NMS-Autostart-Guard-$([guid]::NewGuid().ToString('N'))"
    $guardShell = $shells[0]
    $guardInfo = New-Object System.Diagnostics.ProcessStartInfo
    $guardInfo.FileName = $guardShell
    $guardInfo.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -DryRun -Port {1} -Target 192.0.2.1 -LogPath "{2}" -MutexName "{3}"' -f `
        $supervisor, $guardPort, $guardLog, $guardMutex
    $guardInfo.WorkingDirectory = (Split-Path -Parent $PSScriptRoot)
    $guardInfo.UseShellExecute = $false
    $guardInfo.CreateNoWindow = $true
    $guardInfo.RedirectStandardOutput = $true
    $guardInfo.RedirectStandardError = $true
    $guard = New-Object System.Diagnostics.Process
    $guard.StartInfo = $guardInfo
    $null = $guard.Start()
    $guardErrTask = $guard.StandardError.ReadToEndAsync()
    $null = $guard.StandardOutput.ReadToEndAsync()
    # PowerShell 5.1 bisa butuh >10 detik untuk start saat mesin sibuk.
    $guardExited = $guard.WaitForExit(60000)
    if (-not $guardExited) {
        $guard.Kill()
        $guard.Dispose()
        throw "-DryRun tanpa -Once tidak berhenti dalam 60 detik; seharusnya ditolak"
    }
    if ($guard.ExitCode -ne 2) {
        $guardError = $guardErrTask.Result
        $guard.Dispose()
        throw "-DryRun tanpa -Once harus exit 2, dapat $($guard.ExitCode): $guardError"
    }
    if ($guardErrTask.Result -notmatch 'DryRun hanya didukung') {
        $guard.Dispose()
        throw "-DryRun tanpa -Once tidak memberi pesan penolakan yang jelas"
    }
    $guard.Dispose()
    if (Test-PortInUse -Port $guardPort) { throw "-DryRun tanpa -Once malah menyalakan NMS" }

    # 10. Log supervisor dirotasi dan log serve lama dipangkas.
    $rotateLog = Join-Path $tempDir 'rotate.log'
    [System.IO.File]::WriteAllText($rotateLog, ('x' * 1100000))
    Write-SupervisorLog -Message 'rotation-check' -LogPath $rotateLog
    if (-not (Test-Path -LiteralPath ($rotateLog + '.1'))) { throw "Log supervisor tidak dirotasi" }
    if ((Get-Item -LiteralPath $rotateLog).Length -ge 1048576) { throw "Log supervisor baru masih terlalu besar" }
    if ((Get-Content -LiteralPath $rotateLog -Raw) -notmatch 'rotation-check') {
        throw "Log supervisor baru tidak berisi pesan"
    }

    $serveLogDir = Join-Path $tempDir 'serve-logs'
    New-Item -ItemType Directory -Path $serveLogDir | Out-Null
    foreach ($index in 1..30) {
        Set-Content -LiteralPath (Join-Path $serveLogDir ("nms-serve-20260101-{0:D2}.out.log" -f $index)) -Value 'x'
        Set-Content -LiteralPath (Join-Path $serveLogDir ("nms-serve-20260101-{0:D2}.err.log" -f $index)) -Value 'x'
    }
    Remove-OldServeLogs -LogDirectory $serveLogDir -KeepRuns 10
    $remaining = @(Get-ChildItem -LiteralPath $serveLogDir -Filter 'nms-serve-*.log')
    if ($remaining.Count -ne 20) { throw "Prune serve log salah: $($remaining.Count), expected 20" }

    # 11. Installer dry-run tidak boleh menulis shortcut.
    $dry = & $installer -StartupDir $tempDir -NoStart -DryRun 6>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "Installer dry-run gagal (exit $LASTEXITCODE)`n$dry" }
    $launcher = Join-Path $tempDir 'NMS-Kopegtel.lnk'
    if (Test-Path -LiteralPath $launcher) { throw "Installer dry-run menulis shortcut" }

    # 12. Installer membuat shortcut yang menunjuk wrapper; -Remove menghapusnya.
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

    # 13. -Remove tidak boleh menghapus shortcut milik aplikasi lain.
    $foreign = $shell.CreateShortcut($launcher)
    $foreign.TargetPath = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $foreign.Arguments = '/c exit 0'
    $foreign.Save()
    & $installer -StartupDir $tempDir -Remove 6>&1 | Out-Null
    if ($LASTEXITCODE -ne 1) { throw "Remove shortcut asing harus exit 1, dapat $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $launcher)) { throw "Shortcut asing ikut terhapus" }
    Remove-Item -LiteralPath $launcher -Force

    # 14. Jalur start/health/restart nyata dengan DB sementara (bukan mock).
    $repoRoot = Split-Path -Parent $PSScriptRoot
    if (-not (Get-PythonPath -Root $repoRoot)) { throw "Python untuk start nyata tidak ditemukan" }
    $realPort = Get-FreePort
    $realLog = Join-Path $tempDir 'real-start.log'
    $realServeLogs = Join-Path $tempDir 'real-serve-logs'
    $env:NMS_DB_PATH = Join-Path $tempDir 'real.db'
    $env:DB_BACKUP_ENABLED = '0'
    Remove-Item Env:PORT -ErrorAction SilentlyContinue

    $first = Invoke-SupervisorCycle -Port $realPort -Target '127.0.0.1' `
        -RepoRoot $repoRoot -LogPath $realLog -ServeLogDirectory $realServeLogs
    if ($first.State -ne 'started') {
        throw "Start nyata gagal: state=$($first.State)`n$(Get-Content -LiteralPath $realLog -Raw -ErrorAction SilentlyContinue)"
    }
    $realProcess = $first.Process
    if (-not (Test-HealthEndpoint -Port $realPort)) { throw "NMS hasil start nyata tidak sehat di port $realPort" }

    Stop-Process -Id $realProcess.Id -Force
    $null = $realProcess.WaitForExit(10000)
    for ($wait = 0; $wait -lt 30 -and (Test-PortInUse -Port $realPort); $wait++) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-PortInUse -Port $realPort) { throw "Port $realPort belum bebas setelah proses dihentikan" }

    # Lease instance basi baru bisa diambil alih setelah >30 detik tanpa heartbeat.
    Start-Sleep -Seconds 31
    $second = Invoke-SupervisorCycle -Port $realPort -Target '127.0.0.1' `
        -RepoRoot $repoRoot -LogPath $realLog -ServeLogDirectory $realServeLogs
    if ($second.State -ne 'started') {
        throw "Restart nyata gagal: state=$($second.State)`n$(Get-Content -LiteralPath $realLog -Raw -ErrorAction SilentlyContinue)"
    }
    $realProcess = $second.Process
    if (-not (Test-HealthEndpoint -Port $realPort)) { throw "NMS hasil restart tidak sehat di port $realPort" }

    Stop-Process -Id $realProcess.Id -Force
    $null = $realProcess.WaitForExit(10000)
    $realProcess = $null

    Write-Host 'OK: autostart smoke test valid'
} finally {
    if ($listener) { $listener.Stop() }
    if ($realProcess -and -not $realProcess.HasExited) {
        Stop-Process -Id $realProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($savedEnvPort) {
        $env:PORT = $savedEnvPort
    } else {
        Remove-Item Env:PORT -ErrorAction SilentlyContinue
    }
    if ($savedEnvDb) {
        $env:NMS_DB_PATH = $savedEnvDb
    } else {
        Remove-Item Env:NMS_DB_PATH -ErrorAction SilentlyContinue
    }
    if ($savedEnvBackup) {
        $env:DB_BACKUP_ENABLED = $savedEnvBackup
    } else {
        Remove-Item Env:DB_BACKUP_ENABLED -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

[CmdletBinding()]
param(
    [ValidateRange(0, 65535)]
    [int]$Port = 0,
    [string]$Target = '192.168.30.1',
    [string]$RepoRoot,
    [string]$LogPath,
    [string]$MutexName = 'Local\NMS-Kopegtel-Supervisor',
    [ValidateRange(1, 86400)]
    [int]$IntervalSeconds = 30,
    [switch]$Once,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot belum terisi saat parameter default dievaluasi di Windows
# PowerShell 5.1; resolve di badan script agar shortcut -File juga bekerja.
if (-not $RepoRoot) {
    if ($PSScriptRoot) {
        $RepoRoot = Split-Path -Parent $PSScriptRoot
    } else {
        $RepoRoot = (Get-Location).Path
    }
}

# Supervisor autostart NMS Kopegtel.
# Dipanggil dari shortcut Startup folder. Tugasnya:
# - mencegah instance ganda (mutex + cek port),
# - tidak menyalakan NMS saat perangkat target tidak terjangkau,
# - menyalakan ulang NMS jika proses berhenti.

function Write-SupervisorLog {
    param(
        [string]$Message,
        [string]$LogPath,
        [int]$MaxBytes = 1048576
    )
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if ($LogPath) {
        $directory = Split-Path -Parent $LogPath
        if ($directory -and -not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        if ((Test-Path -LiteralPath $LogPath) -and
            ((Get-Item -LiteralPath $LogPath).Length -ge $MaxBytes)) {
            Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
        }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
}

function Get-ConfiguredPort {
    param(
        [string]$RepoRoot,
        [int]$Override = 0
    )
    if ($Override -gt 0) { return $Override }

    if ($env:PORT) {
        $parsed = 0
        if ([int]::TryParse($env:PORT.Trim(), [ref]$parsed) -and
            $parsed -ge 1 -and $parsed -le 65535) {
            return $parsed
        }
    }

    if ($RepoRoot) {
        $envFile = Join-Path $RepoRoot '.env'
        if (Test-Path -LiteralPath $envFile) {
            foreach ($line in Get-Content -LiteralPath $envFile) {
                $match = [regex]::Match($line, '^\s*(?:export\s+)?PORT\s*=\s*[''"]?(\d+)')
                if ($match.Success) {
                    $parsed = [int]$match.Groups[1].Value
                    if ($parsed -ge 1 -and $parsed -le 65535) { return $parsed }
                }
            }
        }
    }

    return 5500
}

function Test-HealthEndpoint {
    param(
        [int]$Port,
        [string]$Address = '127.0.0.1',
        [int]$TimeoutMs = 3000
    )
    $request = $null
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.WebRequest]::Create("http://${Address}:$Port/healthz")
        $request.Method = 'GET'
        $request.Timeout = $TimeoutMs
        $request.Proxy = $null
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { return $false }
        $reader = New-Object System.IO.StreamReader -ArgumentList $response.GetResponseStream()
        $body = $reader.ReadToEnd()
        return ($body -match '"status"\s*:\s*"ok"')
    } catch {
        return $false
    } finally {
        if ($reader) { $reader.Close() }
        if ($response) { $response.Close() }
    }
}

function Test-NetworkTarget {
    param(
        [string]$Target,
        [int]$TimeoutMs = 3000
    )
    $ping = $null
    try {
        $ping = New-Object System.Net.NetworkInformation.Ping
        $reply = $ping.Send($Target, $TimeoutMs)
        return ($reply -and $reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success)
    } catch {
        return $false
    } finally {
        if ($ping) { $ping.Dispose() }
    }
}

function Test-PortInUse {
    param(
        [int]$Port,
        [string]$Address = '127.0.0.1',
        [int]$TimeoutMs = 500
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($Address, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-PythonPath {
    param([string]$Root)
    if (-not $Root) { $Root = (Get-Location).Path }
    $parent = Split-Path -Parent $Root
    $leaf = Split-Path -Leaf $Root
    $candidates = @(
        (Join-Path $parent ($leaf + '-venv\Scripts\python.exe')),
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notmatch '\\WindowsApps\\') { return $command.Source }
    return $null
}

function Remove-OldServeLogs {
    param(
        [string]$LogDirectory,
        [int]$KeepRuns = 10
    )
    if (-not $LogDirectory -or -not (Test-Path -LiteralPath $LogDirectory)) { return }
    $files = @(Get-ChildItem -LiteralPath $LogDirectory -Filter 'nms-serve-*.log' -File |
        Sort-Object LastWriteTime -Descending)
    $limit = $KeepRuns * 2
    if ($files.Count -le $limit) { return }
    $files | Select-Object -Skip $limit | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Enter-SupervisorMutex {
    param([string]$Name)
    if (-not $Name) { return $null }
    $mutex = New-Object System.Threading.Mutex($false, $Name)
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $acquired = $true
    }
    if ($acquired) { return $mutex }
    $mutex.Dispose()
    return $null
}

function Invoke-SupervisorCycle {
    param(
        [int]$Port,
        [string]$Target,
        [string]$RepoRoot,
        [string]$LogPath,
        [string]$ServeLogDirectory,
        [switch]$DryRun,
        [switch]$Quiet,
        [scriptblock]$HealthProbe,
        [scriptblock]$NetworkProbe
    )

    if (-not $HealthProbe) {
        $HealthProbe = { Test-HealthEndpoint -Port $Port }.GetNewClosure()
    }
    if (-not $NetworkProbe) {
        $NetworkProbe = { Test-NetworkTarget -Target $Target }.GetNewClosure()
    }

    if (& $HealthProbe) {
        if (-not $Quiet) {
            Write-SupervisorLog -Message "NMS sudah sehat di port $Port; tidak menyalakan instance kedua." -LogPath $LogPath
        }
        return [pscustomobject]@{ Code = 0; Process = $null; State = 'healthy' }
    }

    if (-not (& $NetworkProbe)) {
        if (-not $Quiet) {
            Write-SupervisorLog -Message "Target $Target belum terjangkau dari jaringan ini; NMS tidak dinyalakan agar data tidak tercatat offline palsu." -LogPath $LogPath
        }
        return [pscustomobject]@{ Code = 0; Process = $null; State = 'unreachable' }
    }

    if (Test-PortInUse -Port $Port) {
        Write-SupervisorLog -Message "Port $Port dipakai proses lain yang tidak sehat; supervisor tidak menyalakan instance baru." -LogPath $LogPath
        return [pscustomobject]@{ Code = 1; Process = $null; State = 'port-busy' }
    }

    $python = Get-PythonPath -Root $RepoRoot
    if (-not $python) {
        Write-SupervisorLog -Message "Python tidak ditemukan untuk $RepoRoot; pasang dependensi atau jalankan installer dari folder proyek." -LogPath $LogPath
        return [pscustomobject]@{ Code = 1; Process = $null; State = 'error' }
    }

    $arguments = @('backend/serve.py', '--port', [string]$Port)
    $commandLine = '"{0}" {1}' -f $python, ($arguments -join ' ')
    if ($DryRun) {
        Write-SupervisorLog -Message "DryRun: $commandLine (working directory $RepoRoot)" -LogPath $LogPath
        return [pscustomobject]@{ Code = 0; Process = $null; State = 'dry-run' }
    }

    if ($ServeLogDirectory) {
        $logDirectory = $ServeLogDirectory
    } else {
        $logDirectory = Join-Path $RepoRoot 'logs'
    }
    if (-not (Test-Path -LiteralPath $logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stdout = Join-Path $logDirectory "nms-serve-$stamp.out.log"
    $stderr = Join-Path $logDirectory "nms-serve-$stamp.err.log"

    Write-SupervisorLog -Message "Menyalakan NMS: $commandLine" -LogPath $LogPath
    try {
        $process = Start-Process -FilePath $python -ArgumentList $arguments `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        Remove-OldServeLogs -LogDirectory $logDirectory -KeepRuns 10
    } catch {
        Write-SupervisorLog -Message "Gagal menyalakan NMS: $($_.Exception.Message)" -LogPath $LogPath
        return [pscustomobject]@{ Code = 1; Process = $null; State = 'error' }
    }

    # Run pertama bisa memakan ~45 detik (bootstrap DB/admin), jadi tunggu
    # sampai ~90 detik sebelum menyatakan gagal.
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        if ($process.HasExited) {
            Write-SupervisorLog -Message "NMS berhenti saat startup (exit $($process.ExitCode)); lihat $stderr." -LogPath $LogPath
            return [pscustomobject]@{ Code = 1; Process = $null; State = 'error' }
        }
        if (& $HealthProbe) {
            Write-SupervisorLog -Message "NMS sehat setelah dinyalakan (PID $($process.Id)); log: $stdout" -LogPath $LogPath
            return [pscustomobject]@{ Code = 0; Process = $process; State = 'started' }
        }
        Start-Sleep -Milliseconds 1000
    }

    Write-SupervisorLog -Message "NMS belum sehat setelah menunggu ~90 detik (PID $($process.Id)); lihat $stderr." -LogPath $LogPath
    return [pscustomobject]@{ Code = 1; Process = $process; State = 'unhealthy' }
}

function Invoke-Supervisor {
    param(
        [switch]$Once,
        [switch]$DryRun,
        [int]$Port,
        [string]$Target,
        [string]$RepoRoot,
        [string]$LogPath,
        [string]$MutexName,
        [scriptblock]$HealthProbe,
        [scriptblock]$NetworkProbe
    )

    if (-not $RepoRoot) { $RepoRoot = $script:RepoRoot }
    if (-not $Target) { $Target = $script:Target }
    $Port = Get-ConfiguredPort -RepoRoot $RepoRoot -Override $Port

    $mutex = Enter-SupervisorMutex -Name $MutexName
    if (-not $mutex) {
        Write-SupervisorLog -Message 'Supervisor lain sudah berjalan; instance ini berhenti.' -LogPath $LogPath
        return 0
    }

    try {
        $cycle = Invoke-SupervisorCycle -Port $Port -Target $Target -RepoRoot $RepoRoot `
            -LogPath $LogPath -DryRun:$DryRun -HealthProbe $HealthProbe -NetworkProbe $NetworkProbe
        return [int]$cycle.Code
    } finally {
        try { $mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
}

function Invoke-SupervisorLoop {
    param(
        [int]$Port,
        [string]$Target,
        [string]$RepoRoot,
        [string]$LogPath,
        [string]$MutexName,
        [int]$IntervalSeconds
    )

    if (-not $RepoRoot) { $RepoRoot = $script:RepoRoot }
    if (-not $Target) { $Target = $script:Target }
    $Port = Get-ConfiguredPort -RepoRoot $RepoRoot -Override $Port

    $mutex = Enter-SupervisorMutex -Name $MutexName
    if (-not $mutex) {
        Write-SupervisorLog -Message 'Supervisor lain sudah berjalan; instance ini berhenti.' -LogPath $LogPath
        return 0
    }

    Write-SupervisorLog -Message "Supervisor NMS aktif (port $Port, target $Target, cek tiap $IntervalSeconds detik)." -LogPath $LogPath

    $lastState = $null
    try {
        while ($true) {
            try {
                $cycle = Invoke-SupervisorCycle -Port $Port -Target $Target -RepoRoot $RepoRoot `
                    -LogPath $LogPath -Quiet
            } catch {
                Write-SupervisorLog -Message "Siklus supervisor gagal: $($_.Exception.Message)" -LogPath $LogPath
                $cycle = [pscustomobject]@{ Code = 1; Process = $null; State = 'error' }
            }

            if ($cycle.State -eq 'healthy' -and $lastState -ne 'healthy') {
                Write-SupervisorLog -Message "NMS sudah sehat di port $Port; tidak menyalakan instance kedua." -LogPath $LogPath
            } elseif ($cycle.State -eq 'unreachable' -and $lastState -ne 'unreachable') {
                Write-SupervisorLog -Message "Target $Target belum terjangkau dari jaringan ini; NMS tidak dinyalakan agar data tidak tercatat offline palsu." -LogPath $LogPath
            }

            if ($cycle.State -eq 'started') {
                $lastState = 'healthy'
            } else {
                $lastState = $cycle.State
            }

            if ($cycle.Process) {
                $cycle.Process.WaitForExit()
                Write-SupervisorLog -Message "NMS berhenti (exit $($cycle.Process.ExitCode)); mencoba lagi dalam $IntervalSeconds detik." -LogPath $LogPath
                $lastState = 'exited'
            }

            Start-Sleep -Seconds $IntervalSeconds
        }
    } finally {
        try { $mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($DryRun -and -not $Once) {
        [Console]::Error.WriteLine('-DryRun hanya didukung bersama -Once; tidak ada NMS yang dinyalakan.')
        exit 2
    }
    if (-not $LogPath) {
        $LogPath = Join-Path (Join-Path $RepoRoot 'logs') 'nms-autostart.log'
    }
    if ($Once) {
        exit (Invoke-Supervisor -Once -DryRun:$DryRun -Port $Port -Target $Target -RepoRoot $RepoRoot `
            -LogPath $LogPath -MutexName $MutexName)
    }
    exit (Invoke-SupervisorLoop -Port $Port -Target $Target -RepoRoot $RepoRoot `
        -LogPath $LogPath -MutexName $MutexName -IntervalSeconds $IntervalSeconds)
}

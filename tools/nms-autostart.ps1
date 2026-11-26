[CmdletBinding()]
param(
    [int]$Port = 5000,
    [string]$Target = '192.168.30.1',
    [string]$RepoRoot,
    [string]$LogPath,
    [string]$MutexName = 'Local\NMS-Kopegtel-Supervisor',
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
        [string]$LogPath
    )
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if ($LogPath) {
        $directory = Split-Path -Parent $LogPath
        if ($directory -and -not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
}

function Test-HealthEndpoint {
    param(
        [int]$Port,
        [string]$Address = '127.0.0.1',
        [int]$TimeoutMs = 3000
    )
    $request = $null
    $response = $null
    try {
        $request = [System.Net.WebRequest]::Create("http://${Address}:$Port/healthz")
        $request.Method = 'GET'
        $request.Timeout = $TimeoutMs
        $request.Proxy = $null
        $response = $request.GetResponse()
        return ([int]$response.StatusCode -eq 200)
    } catch {
        return $false
    } finally {
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
    if ($command) { return $command.Source }
    return $null
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
        [switch]$DryRun,
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
        Write-SupervisorLog -Message "NMS sudah sehat di port $Port; tidak menyalakan instance kedua." -LogPath $LogPath
        return [pscustomobject]@{ Code = 0; Process = $null }
    }

    if (-not (& $NetworkProbe)) {
        Write-SupervisorLog -Message "Target $Target belum terjangkau dari jaringan ini; NMS tidak dinyalakan agar data tidak tercatat offline palsu." -LogPath $LogPath
        return [pscustomobject]@{ Code = 0; Process = $null }
    }

    if (Test-PortInUse -Port $Port) {
        Write-SupervisorLog -Message "Port $Port dipakai proses lain yang tidak sehat; supervisor tidak menyalakan instance baru." -LogPath $LogPath
        return [pscustomobject]@{ Code = 1; Process = $null }
    }

    $python = Get-PythonPath -Root $RepoRoot
    if (-not $python) {
        Write-SupervisorLog -Message "Python tidak ditemukan untuk $RepoRoot; pasang dependensi atau jalankan installer dari folder proyek." -LogPath $LogPath
        return [pscustomobject]@{ Code = 1; Process = $null }
    }

    $arguments = @('backend/serve.py', '--port', [string]$Port)
    $commandLine = '"{0}" {1}' -f $python, ($arguments -join ' ')
    if ($DryRun) {
        Write-SupervisorLog -Message "DryRun: $commandLine (working directory $RepoRoot)" -LogPath $LogPath
        return [pscustomobject]@{ Code = 0; Process = $null }
    }

    $logDirectory = Join-Path $RepoRoot 'logs'
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
    } catch {
        Write-SupervisorLog -Message "Gagal menyalakan NMS: $($_.Exception.Message)" -LogPath $LogPath
        return [pscustomobject]@{ Code = 1; Process = $null }
    }

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($process.HasExited) {
            Write-SupervisorLog -Message "NMS berhenti saat startup (exit $($process.ExitCode)); lihat $stderr." -LogPath $LogPath
            return [pscustomobject]@{ Code = 1; Process = $null }
        }
        if (& $HealthProbe) {
            Write-SupervisorLog -Message "NMS sehat setelah dinyalakan (PID $($process.Id)); log: $stdout" -LogPath $LogPath
            return [pscustomobject]@{ Code = 0; Process = $process }
        }
        Start-Sleep -Milliseconds 1000
    }

    Write-SupervisorLog -Message "NMS belum sehat setelah 30 detik (PID $($process.Id)); lihat $stderr." -LogPath $LogPath
    return [pscustomobject]@{ Code = 1; Process = $process }
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

    $mutex = Enter-SupervisorMutex -Name $MutexName
    if (-not $mutex) {
        Write-SupervisorLog -Message 'Supervisor lain sudah berjalan; instance ini berhenti.' -LogPath $LogPath
        return 0
    }

    Write-SupervisorLog -Message "Supervisor NMS aktif (port $Port, target $Target, cek tiap $IntervalSeconds detik)." -LogPath $LogPath

    try {
        while ($true) {
            try {
                $cycle = Invoke-SupervisorCycle -Port $Port -Target $Target -RepoRoot $RepoRoot -LogPath $LogPath
            } catch {
                Write-SupervisorLog -Message "Siklus supervisor gagal: $($_.Exception.Message)" -LogPath $LogPath
                $cycle = [pscustomobject]@{ Code = 1; Process = $null }
            }

            if ($cycle.Process) {
                $cycle.Process.WaitForExit()
                Write-SupervisorLog -Message "NMS berhenti (exit $($cycle.Process.ExitCode)); mencoba lagi dalam $IntervalSeconds detik." -LogPath $LogPath
            }

            Start-Sleep -Seconds $IntervalSeconds
        }
    } finally {
        try { $mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
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

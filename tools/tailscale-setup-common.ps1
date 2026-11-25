# Fungsi bersama untuk setup Tailscale subnet router.
# Dot-source dari office-tailscale-subnet.ps1 dan test_office-tailscale-subnet.ps1.

function New-TailscaleUpArgs {
    param(
        [string]$AuthKeyFile,
        [string]$Subnet,
        [string]$Hostname,
        [switch]$Reset
    )

    # "up" hanya dipanggil saat node belum Running (misalnya NeedsLogin atau
    # Stopped). Saat itu preferensi lama masih tersimpan, jadi semua preferensi
    # yang kita kelola harus disebut; kalau tidak, Tailscale menolak dengan
    # "requires mentioning all non-default flags". "set" tetap dijalankan
    # setelahnya agar hasil akhirnya idempoten.
    $arguments = @(
        'up',
        "--hostname=$Hostname",
        '--accept-routes=false',
        '--unattended',
        "--advertise-routes=$Subnet"
    )
    if ($Reset) { $arguments += '--reset' }
    if ($AuthKeyFile) { $arguments += "--auth-key=file:$AuthKeyFile" }
    return ,$arguments
}

function Test-ShouldRunTailscaleUp {
    param(
        [string]$BackendState,
        [switch]$Reset
    )

    # "up" diperlukan saat node belum Running. Setelah Running, "set" saja yang
    # dijalankan agar tidak memicu error flag non-default. Jika state tidak
    # terbaca, coba "up" untuk memulihkan backend.
    if ($Reset) { return $true }
    if (-not $BackendState) { return $true }
    return ($BackendState -ne 'Running')
}

function Test-TailscaleNeedsAuthKey {
    param([string]$BackendState)

    # Login ulang hanya diperlukan saat belum punya identitas. Node Stopped
    # atau NeedsMachineAuth sudah terautentikasi dan tidak butuh auth key.
    if (-not $BackendState) { return $true }
    return ($BackendState -eq 'NeedsLogin' -or $BackendState -eq 'NoState')
}

function New-TailscaleSetArgs {
    param(
        [string]$Subnet,
        [string]$Hostname
    )

    # "tailscale set" hanya mengubah preferensi yang disebut dan idempoten.
    return ,@(
        'set',
        "--hostname=$Hostname",
        '--accept-routes=false',
        '--unattended',
        "--advertise-routes=$Subnet"
    )
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string[]]$Arguments
    )

    if (-not $Path) { throw 'tailscale.exe tidak ditemukan.' }

    # Windows PowerShell 5.1 mengubah stderr native menjadi ErrorRecord dan
    # $ErrorActionPreference='Stop' akan melempar RemoteException. Turunkan
    # EAP hanya selama pemanggilan, lalu kembalikan.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Path @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }

    return [pscustomobject]@{
        Output = @($output)
        ExitCode = $exitCode
    }
}

function Get-TailscaleBackendState {
    param(
        [string]$Path,
        [int]$Attempts = 20,
        [int]$DelaySeconds = 1
    )

    # status --json tetap bisa mengeluarkan JSON saat NeedsLogin, kadang dengan
    # exit code nonzero. Karena itu JSON dibaca dari stdout, bukan dari exit code.
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $result = Invoke-NativeCapture -Path $Path -Arguments @('status', '--json')
        $text = ($result.Output |
            Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
            ForEach-Object { [string]$_ }) -join "`n"

        if ($text.Trim()) {
            try {
                $state = [string](($text | ConvertFrom-Json).BackendState)
                if ($state -and $state -notin @('Starting', 'NoState')) { return $state }
                if ($state -and ($attempt + 1) -ge $Attempts) { return $state }
            } catch {
                # Output belum JSON yang valid; tunggu service selesai start.
            }
        }

        if (($attempt + 1) -lt $Attempts) { Start-Sleep -Seconds $DelaySeconds }
    }
    return $null
}

function Format-TailscaleCommand {
    param([string[]]$Arguments)

    $display = foreach ($argument in $Arguments) {
        if ($argument -like '--auth-key=*') {
            '--auth-key=file:<REDACTED>'
        } else {
            $argument
        }
    }
    return 'tailscale ' + ($display -join ' ')
}

function Invoke-TailscaleCommand {
    param(
        [string]$TailscalePath,
        [string[]]$Arguments,
        [string]$Redact
    )

    if (-not $TailscalePath) { throw 'tailscale.exe tidak ditemukan.' }

    Write-Host ("Perintah: " + (Format-TailscaleCommand -Arguments $Arguments))
    # Gunakan Invoke-NativeCapture agar stderr native tidak melempar
    # RemoteException di Windows PowerShell 5.1.
    $result = Invoke-NativeCapture -Path $TailscalePath -Arguments $Arguments

    foreach ($line in $result.Output) {
        $text = [string]$line
        if ($Redact) { $text = $text.Replace($Redact, '<REDACTED>') }
        Write-Host $text
    }

    if ($result.ExitCode -ne 0) {
        throw "tailscale $($Arguments[0]) gagal (exit $($result.ExitCode))."
    }
}

function Test-IPForwardingEnabled {
    $parameters = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters'
    $value = (Get-ItemProperty -Path $parameters -Name IPEnableRouter -ErrorAction SilentlyContinue).IPEnableRouter
    return ($value -eq 1)
}

function New-ProtectedAuthKeyFile {
    param([string]$AuthKey)

    $path = Join-Path $env:TEMP ("tailscale-auth-{0}.txt" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($path, '')

    try {
        $acl = Get-Acl -Path $path
        $acl.SetAccessRuleProtection($true, $false)
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object -TypeName Security.AccessControl.FileSystemAccessRule `
            -ArgumentList $identity, 'FullControl', 'Allow'
        $acl.AddAccessRule($rule)
        Set-Acl -Path $path -AclObject $acl
        [IO.File]::WriteAllText($path, $AuthKey, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        throw "Gagal membuat file auth key sementara: $($_.Exception.Message)"
    }

    return $path
}

function Read-AuthKeySecure {
    $secure = Read-Host -AsSecureString 'Tempel auth key Tailscale (input tidak ditampilkan)'
    if (-not $secure -or $secure.Length -eq 0) { return '' }

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

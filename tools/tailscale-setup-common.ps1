# Fungsi bersama untuk setup Tailscale subnet router.
# Dot-source dari office-tailscale-subnet.ps1 dan test_office-tailscale-subnet.ps1.

function New-TailscaleUpArgs {
    param(
        [string]$AuthKeyFile,
        [switch]$Reset
    )

    # Login-only "up": preferensi diatur terpisah dengan "tailscale set" agar
    # aman dijalankan ulang tanpa error "requires mentioning all non-default flags".
    $arguments = @('up')
    if ($Reset) { $arguments += '--reset' }
    if ($AuthKeyFile) { $arguments += "--auth-key=file:$AuthKeyFile" }
    return ,$arguments
}

function Test-ShouldRunTailscaleUp {
    param(
        [bool]$LoggedIn,
        [switch]$Reset
    )

    # Setelah login, "tailscale up" tanpa flag bisa ditolak karena preferensi
    # non-default dari "tailscale set" belum disebut. Jadi lewati "up" kecuali
    # memang belum login atau user meminta reset.
    if ($LoggedIn -and -not $Reset) { return $false }
    return $true
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
    $output = & $TailscalePath @Arguments 2>&1
    $exitCode = $LASTEXITCODE

    foreach ($line in $output) {
        $text = [string]$line
        if ($Redact) { $text = $text.Replace($Redact, '<REDACTED>') }
        Write-Host $text
    }

    if ($exitCode -ne 0) {
        throw "tailscale $($Arguments[0]) gagal (exit $exitCode)."
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

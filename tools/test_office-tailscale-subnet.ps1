$ErrorActionPreference = 'Stop'

$script = Join-Path $PSScriptRoot 'office-tailscale-subnet.ps1'
$common = Join-Path $PSScriptRoot 'tailscale-setup-common.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "Script belum ada: $script" }
if (-not (Test-Path -LiteralPath $common)) { throw "Modul belum ada: $common" }

. $common

function Invoke-ChildShell {
    param(
        [string]$Shell,
        [string[]]$Arguments
    )

    $raw = & $Shell -NoProfile -ExecutionPolicy Bypass @Arguments 2>&1
    return [pscustomobject]@{
        Output   = ($raw | Out-String)
        ExitCode = $LASTEXITCODE
    }
}

$shells = @()
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) { $shells += $pwsh.Source }
$winps = Get-Command powershell.exe -ErrorAction SilentlyContinue
if ($winps -and ($shells -notcontains $winps.Source)) { $shells += $winps.Source }
if ($shells.Count -eq 0) { throw 'Tidak ada PowerShell yang bisa dipakai untuk smoke test.' }

$fakeKey = 'tskey-auth-TESTONLY1234567890'
$tempDir = Join-Path $env:TEMP ("tailscale-test-{0}" -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null

try {
    # 1. Dry-run harus aman, lengkap, dan tidak membocorkan auth key.
    $dryRunKeyFile = Join-Path $tempDir 'dryrun-key.txt'
    Set-Content -LiteralPath $dryRunKeyFile -Value $fakeKey -NoNewline

    foreach ($shell in $shells) {
        $result = Invoke-ChildShell -Shell $shell -Arguments @(
            '-File', $script, '-DryRun', '-AuthKeyFile', $dryRunKeyFile
        )
        if ($result.ExitCode -ne 0) {
            throw "Dry-run gagal di $shell (exit $($result.ExitCode))`n$($result.Output)"
        }

        $output = $result.Output
        if ($output -notmatch 'tailscale up') { throw "Dry-run tidak menampilkan perintah tailscale up" }
        if ($output -notmatch 'tailscale set') { throw "Dry-run tidak menampilkan perintah tailscale set" }
        if ($output -notmatch '--hostname=kopegtel-office') { throw "Dry-run tidak menampilkan hostname" }
        if ($output -notmatch '--accept-routes=false') { throw "Dry-run tidak menampilkan --accept-routes=false" }
        if ($output -notmatch '--unattended') { throw "Dry-run tidak menampilkan --unattended" }
        if ($output -notmatch '--advertise-routes=192\.168\.30\.0/24') { throw "Dry-run tidak menampilkan subnet" }
        if ($output -notmatch 'IP forward') { throw "Dry-run tidak menampilkan status IP forwarding" }
        if ($output -match 'tskey-auth-[A-Za-z0-9]+') { throw "Dry-run membocorkan auth key" }

        $upLine = ($output -split "`r?`n" | Where-Object { $_ -match 'Perintah: tailscale up' } | Select-Object -First 1)
        if (-not $upLine) { throw "Dry-run tidak menampilkan baris tailscale up" }
        if ($upLine -notmatch '--auth-key=file:<REDACTED>') { throw "Auth key pada tailscale up tidak disamarkan" }
        if ($upLine -notmatch '--advertise-routes=192\.168\.30\.0/24') {
            throw "tailscale up harus menyebut semua preferensi non-default"
        }
    }

    # 2. Argumen harus persis dan tidak memuat auth key mentah.
    $upArgs = New-TailscaleUpArgs -AuthKeyFile 'C:\temp\key.txt' -Subnet '192.168.30.0/24' -Hostname 'kopegtel-office' -Reset
    $expectedUp = 'up --hostname=kopegtel-office --accept-routes=false --unattended --advertise-routes=192.168.30.0/24 --reset --auth-key=file:C:\temp\key.txt'
    if (($upArgs -join ' ') -ne $expectedUp) {
        throw "Argumen tailscale up salah: $($upArgs -join ' ')"
    }
    if (($upArgs -join ' ') -match 'tskey-auth-') { throw "Auth key masuk ke argv" }

    $setArgs = New-TailscaleSetArgs -Subnet '192.168.30.0/24' -Hostname 'kopegtel-office'
    $expectedSet = 'set --hostname=kopegtel-office --accept-routes=false --unattended --advertise-routes=192.168.30.0/24'
    if (($setArgs -join ' ') -ne $expectedSet) {
        throw "Argumen tailscale set salah: $($setArgs -join ' ')"
    }

    # 3. File auth key sementara harus berisi key dan mewarisi ACL yang dibatasi.
    $protectedFile = New-ProtectedAuthKeyFile -AuthKey $fakeKey
    try {
        $content = (Get-Content -LiteralPath $protectedFile -Raw).Trim()
        if ($content -ne $fakeKey) { throw "Isi file auth key sementara salah" }
        $acl = Get-Acl -LiteralPath $protectedFile
        if (-not $acl.AreAccessRulesProtected) { throw "ACL file auth key masih mewarisi izin" }
    } finally {
        Remove-Item -LiteralPath $protectedFile -Force -ErrorAction SilentlyContinue
    }

    # 4. Putusan up/up-auth-key harus berdasarkan BackendState.
    if (Test-ShouldRunTailscaleUp -BackendState 'Running') {
        throw "Script masih menjalankan tailscale up saat node Running"
    }
    if (-not (Test-ShouldRunTailscaleUp -BackendState 'Running' -Reset)) {
        throw "Script tidak menjalankan tailscale up saat -Reset"
    }
    foreach ($state in @('NeedsLogin', 'Stopped', 'Starting', 'NoState', '')) {
        if (-not (Test-ShouldRunTailscaleUp -BackendState $state)) {
            throw "Script tidak menjalankan tailscale up saat state '$state'"
        }
    }
    foreach ($state in @('NeedsLogin', 'NoState', '')) {
        if (-not (Test-TailscaleNeedsAuthKey -BackendState $state)) {
            throw "State '$state' seharusnya meminta auth key"
        }
    }
    foreach ($state in @('Running', 'Stopped', 'NeedsMachineAuth')) {
        if (Test-TailscaleNeedsAuthKey -BackendState $state) {
            throw "State '$state' tidak boleh meminta auth key"
        }
    }

    # 5. CLI palsu yang menulis stderr dan status NeedsLogin tidak boleh melempar
    #    di Windows PowerShell 5.1.
    $stderrCli = Join-Path $tempDir 'tailscale-stderr.cmd'
    $statusCli = Join-Path $tempDir 'tailscale-status.cmd'
    @'
@echo off
echo Waiting for authentication... 1>&2
exit /b 0
'@ | Set-Content -Path $stderrCli -Encoding ASCII
    @'
@echo off
echo {"BackendState":"NeedsLogin"}
echo no current Tailscale IPs; state: NeedsLogin 1>&2
exit /b 1
'@ | Set-Content -Path $statusCli -Encoding ASCII

    $nativeTest = Join-Path $tempDir 'native-capture-test.ps1'
    @'
param([string]$CommonPath, [string]$StderrCli, [string]$StatusCli)
$ErrorActionPreference = 'Stop'
. $CommonPath
$result = Invoke-NativeCapture -Path $StderrCli -Arguments @('up')
if ($result.ExitCode -ne 0) { throw "exit=$($result.ExitCode)" }
$text = ($result.Output | ForEach-Object { [string]$_ }) -join "`n"
if ($text -notmatch 'Waiting for authentication') { throw "stderr tidak tertangkap: $text" }
$state = Get-TailscaleBackendState -Path $StatusCli -Attempts 1
if ($state -ne 'NeedsLogin') { throw "state=$state" }
Write-Host 'native-ok'
'@ | Set-Content -Path $nativeTest -Encoding ASCII

    foreach ($shell in $shells) {
        $result = Invoke-ChildShell -Shell $shell -Arguments @(
            '-File', $nativeTest, $common, $stderrCli, $statusCli
        )
        if ($result.ExitCode -ne 0 -or $result.Output -notmatch 'native-ok') {
            throw "Native capture gagal di $shell (exit $($result.ExitCode))`n$($result.Output)"
        }
    }

    # 6. Jalankan script asli dua kali terhadap CLI palsu. Run pertama harus
    #    login dengan up + set, run kedua hanya set.
    $fakeCli = Join-Path $tempDir 'tailscale.cmd'
    $log = Join-Path $tempDir 'args.log'
    $stateFile = Join-Path $tempDir 'state.json'
    @'
@echo off
echo %*>>"%FAKE_TS_LOG%"
if "%1"=="status" if "%2"=="--json" (
  type "%FAKE_TS_STATE%"
  exit /b 0
)
if "%1"=="up" (
  echo Waiting for authentication... 1>&2
  >"%FAKE_TS_STATE%" echo {"BackendState":"Running"}
  exit /b 0
)
if "%1"=="ip" (
  echo 100.64.0.10
  exit /b 0
)
if "%1"=="status" (
  echo 100.64.0.10 kopegtel-office
  exit /b 0
)
exit /b 0
'@ | Set-Content -Path $fakeCli -Encoding ASCII

    $keyFile = Join-Path $tempDir 'auth-key.txt'
    Set-Content -LiteralPath $keyFile -Value $fakeKey -NoNewline

    $env:FAKE_TS_LOG = $log
    $env:FAKE_TS_STATE = $stateFile
    try {
        foreach ($shell in $shells) {
            Set-Content -LiteralPath $stateFile -Value '{"BackendState":"NeedsLogin"}' -NoNewline
            Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue

            $beforeKeys = @(Get-ChildItem -Path $env:TEMP -Filter 'tailscale-auth-*.txt' -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName)

            $runArgs = @(
                '-File', $script,
                '-TailscalePath', $fakeCli,
                '-AuthKeyFile', $keyFile,
                '-SkipAdminCheck', '-SkipForwardingCheck', '-SkipServiceCheck', '-SkipTargetCheck'
            )
            $first = Invoke-ChildShell -Shell $shell -Arguments $runArgs
            if ($first.ExitCode -ne 0) {
                throw "Run pertama gagal di $shell (exit $($first.ExitCode))`n$($first.Output)"
            }
            if ($first.Output -notmatch 'Status backend Tailscale: NeedsLogin') {
                throw "Run pertama tidak membaca state NeedsLogin di $shell`n$($first.Output)"
            }

            $firstLines = @(Get-Content -LiteralPath $log | Where-Object { $_ -match '^(up|set) ' })
            if ($firstLines.Count -ne 2) {
                throw "Run pertama memanggil up/set $($firstLines.Count) kali, expected 2 di $shell"
            }
            if ($firstLines[0] -notmatch '^up ') { throw "Run pertama tidak memanggil up di $shell" }
            if ($firstLines[0] -notmatch '--advertise-routes=192\.168\.30\.0/24') {
                throw "up tidak menyebut advertise-routes di $shell"
            }
            if ($firstLines[0] -notmatch '--auth-key=file:') { throw "up tidak memakai auth key file di $shell" }
            if ($firstLines[0] -match 'tskey-auth-') { throw "Auth key bocor ke argv di $shell" }
            if ($firstLines[1] -notmatch '^set ') { throw "Run pertama tidak memanggil set di $shell" }

            $afterKeys = @(Get-ChildItem -Path $env:TEMP -Filter 'tailscale-auth-*.txt' -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName)
            $leftover = @($afterKeys | Where-Object { $beforeKeys -notcontains $_ })
            if ($leftover.Count -gt 0) {
                throw "File auth key sementara tidak dihapus di ${shell}: $($leftover -join ', ')"
            }

            $second = Invoke-ChildShell -Shell $shell -Arguments $runArgs
            if ($second.ExitCode -ne 0) {
                throw "Run kedua gagal di $shell (exit $($second.ExitCode))`n$($second.Output)"
            }
            if ($second.Output -notmatch 'Status backend Tailscale: Running') {
                throw "Run kedua tidak membaca state Running di $shell`n$($second.Output)"
            }

            $secondLines = @(Get-Content -LiteralPath $log | Where-Object { $_ -match '^(up|set) ' })
            if ($secondLines.Count -ne 3) {
                throw "Run kedua menambah $($secondLines.Count - 2) panggilan up/set, expected 1 set di $shell"
            }
            if ($secondLines[2] -notmatch '^set ') {
                throw "Run kedua tidak memanggil set di $shell"
            }
            if ($secondLines[2] -match '^up ') {
                throw "Run kedua masih memanggil up di $shell"
            }
        }
    } finally {
        Remove-Item Env:FAKE_TS_LOG -ErrorAction SilentlyContinue
        Remove-Item Env:FAKE_TS_STATE -ErrorAction SilentlyContinue
    }

    Write-Host 'OK: smoke test valid'
} finally {
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

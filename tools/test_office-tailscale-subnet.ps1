$ErrorActionPreference = 'Stop'

$script = Join-Path $PSScriptRoot 'office-tailscale-subnet.ps1'
$common = Join-Path $PSScriptRoot 'tailscale-setup-common.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "Script belum ada: $script" }
if (-not (Test-Path -LiteralPath $common)) { throw "Modul belum ada: $common" }

. $common

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
$shell = if ($pwsh) { $pwsh.Source } else { (Get-Command powershell.exe -ErrorAction Stop).Source }
$fakeKey = 'tskey-auth-TESTONLY1234567890'

# 1. Dry-run harus aman, lengkap, dan tidak membocorkan auth key.
$dryRunKeyFile = Join-Path $env:TEMP ("tailscale-dryrun-{0}.txt" -f [guid]::NewGuid().ToString('N'))
Set-Content -LiteralPath $dryRunKeyFile -Value $fakeKey -NoNewline
try {
    $raw = & $shell -NoProfile -ExecutionPolicy Bypass -File $script -DryRun -AuthKeyFile $dryRunKeyFile 2>&1
    $exitCode = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $dryRunKeyFile -Force -ErrorAction SilentlyContinue
}
$output = $raw | Out-String

if ($exitCode -ne 0) { throw "Dry-run gagal dengan exit code $exitCode`n$output" }
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
if ($upLine -match '--advertise-routes') { throw "tailscale up tidak boleh mengubah preferensi; gunakan tailscale set" }

# 2. Argumen harus persis dan tidak memuat auth key mentah.
$upArgs = New-TailscaleUpArgs -AuthKeyFile 'C:\temp\key.txt' -Reset
$expectedUp = 'up --reset --auth-key=file:C:\temp\key.txt'
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

# 4. Eksekusi dengan CLI palsu: pastikan argv yang dikirim benar dan idempoten.
$tempDir = Join-Path $env:TEMP ("tailscale-test-{0}" -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
$fakeCli = Join-Path $tempDir 'tailscale.cmd'
$log = Join-Path $tempDir 'args.log'
@'
@echo %*>>"%FAKE_TS_LOG%"
@exit /b 0
'@ | Set-Content -Path $fakeCli -Encoding ASCII

$env:FAKE_TS_LOG = $log
try {
    Invoke-TailscaleCommand -TailscalePath $fakeCli -Arguments $upArgs -Redact $fakeKey
    Invoke-TailscaleCommand -TailscalePath $fakeCli -Arguments $setArgs
    Invoke-TailscaleCommand -TailscalePath $fakeCli -Arguments $setArgs

    $lines = @(Get-Content -LiteralPath $log)
    if ($lines.Count -ne 3) { throw "CLI palsu dipanggil $($lines.Count) kali, expected 3" }
    if ($lines[0] -ne $expectedUp) { throw "Argv up salah: $($lines[0])" }
    if ($lines[0] -match 'tskey-auth-') { throw "Auth key bocor ke argv" }
    if ($lines[1] -ne $expectedSet) { throw "Argv set pertama salah: $($lines[1])" }
    if ($lines[2] -ne $expectedSet) { throw "Argv set kedua salah (tidak idempoten): $($lines[2])" }
} finally {
    Remove-Item Env:FAKE_TS_LOG -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

# 5. Putusan "up" harus melewati up setelah login, tetapi tetap jalan saat reset.
if (Test-ShouldRunTailscaleUp -LoggedIn $true) {
    throw "Script masih menjalankan tailscale up setelah login"
}
if (-not (Test-ShouldRunTailscaleUp -LoggedIn $true -Reset)) {
    throw "Script tidak menjalankan tailscale up saat -Reset"
}
if (-not (Test-ShouldRunTailscaleUp -LoggedIn $false)) {
    throw "Script tidak menjalankan tailscale up saat belum login"
}

Write-Host 'OK: smoke test valid'

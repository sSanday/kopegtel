$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'office-tailscale-subnet.ps1'
if (-not (Test-Path $script)) { throw "Script belum ada: $script" }

$hostExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }
$fakeKey = 'tskey-auth-TESTONLY1234567890'
$output = & $hostExe -NoProfile -File $script -DryRun -AuthKey $fakeKey 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "Dry-run gagal dengan exit code $LASTEXITCODE" }
if ($output -notmatch 'tailscale up') { throw "Dry-run tidak menampilkan perintah tailscale up" }
if ($output -notmatch '192\.168\.30\.0/24') { throw "Dry-run tidak menampilkan subnet" }
if ($output -notmatch 'kopegtel-office') { throw "Dry-run tidak menampilkan hostname" }
if ($output -match [regex]::Escape($fakeKey)) { throw "Dry-run membocorkan auth key" }
if ($output -notmatch '<REDACTED>') { throw "Dry-run tidak menandai auth key sebagai REDACTED" }
Write-Host 'OK: dry-run valid'

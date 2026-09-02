# tuimail installer — irm https://raw.githubusercontent.com/alpatovdanila/tui-mail/main/install.ps1 | iex
# Installs the latest release to %LOCALAPPDATA%\Programs\tuimail, sha256-verified.
# Knobs: $env:TUIMAIL_INSTALL_DIR overrides the target dir; $env:TUIMAIL_NO_PATH=1 skips the PATH edit.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = 'alpatovdanila/tui-mail'
$dir = if ($env:TUIMAIL_INSTALL_DIR) { $env:TUIMAIL_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\tuimail' }

Write-Host 'Looking up the latest tuimail release...'
$api = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'tuimail-installer' }
$asset = $api.assets | Where-Object name -eq 'tuimail-windows.exe'
if (-not $asset) { throw 'The latest release has no Windows binary.' }

New-Item -ItemType Directory -Force $dir | Out-Null
$exe = Join-Path $dir 'tuimail.exe'
$tmp = "$exe.download"
Write-Host "Downloading tuimail $($api.tag_name)..."
Invoke-WebRequest $asset.browser_download_url -OutFile $tmp

if ($asset.digest) {
  $want = ($asset.digest -replace '^sha256:', '').ToLower()
  $got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $want) { Remove-Item $tmp; throw 'Integrity check FAILED - not installing.' }
  Write-Host 'sha256 verified.'
} else {
  Write-Warning 'release digest unavailable - relying on TLS only.'
}
Move-Item -Force $tmp $exe

if (-not $env:TUIMAIL_NO_PATH) {
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (($userPath -split ';') -notcontains $dir) {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$dir", 'User')
    Write-Host "Added $dir to your PATH - open a new terminal."
  }
}

Write-Host "Installed tuimail $($api.tag_name) to $exe - run: tuimail"

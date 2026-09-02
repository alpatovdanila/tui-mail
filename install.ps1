# tuimail installer - irm https://raw.githubusercontent.com/alpatovdanila/tui-mail/main/install.ps1 | iex
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
  $parts = @(($userPath -split ';') | Where-Object { $_ })
  # always at the front, even when an earlier install appended it at the end:
  # a stale copy elsewhere (an old pip install in Python\Scripts, say) must
  # not keep winning over the fresh binary
  $rest = @($parts | Where-Object { $_ -ne $dir })
  [Environment]::SetEnvironmentVariable('Path', ((@($dir) + $rest) -join ';'), 'User')
  if ($parts -notcontains $dir) {
    Write-Host "Added $dir to the front of your PATH - open a new terminal."
  } elseif ($parts[0] -ne $dir) {
    Write-Host "Moved $dir to the front of your PATH - open a new terminal."
  }
}

$other = Get-Command tuimail -All -ErrorAction SilentlyContinue |
  Where-Object { $_.Source -and $_.Source -ne $exe } | Select-Object -First 1
if ($other) {
  Write-Warning "Another tuimail is also on your PATH: $($other.Source) - probably an older copy."
  Write-Warning "Remove it (for a Python install: pip uninstall tuimail / pipx uninstall tuimail), or 'tuimail' may keep starting that one."
}

Write-Host "Installed tuimail $($api.tag_name) to $exe - run: tuimail   (tuimail --version shows what runs)"

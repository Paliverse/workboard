# WorkBoard installer for Windows (x64 and arm64).
#
#   irm https://github.com/Paliverse/workboard/releases/latest/download/install.ps1 | iex
#
# Installs the self-contained release binary into %LOCALAPPDATA%\Programs\WorkBoard and adds
# that directory to the user PATH. It never runs `workboard setup`.
#
# Environment:
#   WORKBOARD_VERSION            release to install, e.g. 0.1.0 (default: latest)
#   WORKBOARD_INSTALL_BASE_URL   http(s) URL holding the release assets and SHA256SUMS
#   WORKBOARD_NO_MODIFY_PATH=1   leave the user PATH alone (same as -NoModifyPath)
param(
    [string]$Version = $env:WORKBOARD_VERSION,
    [switch]$NoModifyPath
)

# Child scope: `irm | iex` runs in the caller's session, which must keep its own preferences.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar

    $arch = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
        'X64' { 'x64' }
        'Arm64' { 'arm64' }
        default { throw "WorkBoard ships 64-bit Windows binaries (x64, arm64); this system is $_." }
    }
    $asset = "workboard-windows-$arch.zip"
    $release = "$Version".Trim() -replace '^v', ''
    $base = if ($env:WORKBOARD_INSTALL_BASE_URL) { $env:WORKBOARD_INSTALL_BASE_URL.TrimEnd('/') }
        elseif ($release) { "https://github.com/Paliverse/workboard/releases/download/v$release" }
        else { 'https://github.com/Paliverse/workboard/releases/latest/download' }
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\WorkBoard'
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "workboard-install-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $tmp | Out-Null

    try {
        Write-Host "Downloading $base/$asset"
        $archive = Join-Path $tmp $asset
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile $archive
        Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS" -OutFile (Join-Path $tmp 'SHA256SUMS')
        $expected = foreach ($line in Get-Content (Join-Path $tmp 'SHA256SUMS')) {
            $hash, $name = "$line".Trim() -split '\s+', 2
            if ($name -and $name.TrimStart('*') -eq $asset) { $hash }
        }
        if (-not $expected) { throw "SHA256SUMS has no entry for $asset." }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash
        if ($actual -ne $expected) { throw "Checksum mismatch for ${asset}: expected $expected, got $actual." }

        Expand-Archive -LiteralPath $archive -DestinationPath $tmp
        $staged = Join-Path $tmp 'workboard'
        Copy-Item (Join-Path $staged 'workboard.exe') (Join-Path $staged 'wb.exe')

        # A running workboard.exe (for example `workboard upgrade`) locks its files, but its
        # directory can still be renamed: move the old install aside, delete it when possible.
        Get-ChildItem -Path (Split-Path $installDir) -Filter 'WorkBoard.old-*' -Directory -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $installDir) {
            $old = "$installDir.old-$([guid]::NewGuid().ToString('N'))"
            Move-Item -LiteralPath $installDir -Destination $old
            Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Path (Split-Path $installDir) -Force | Out-Null
        Move-Item -LiteralPath $staged -Destination $installDir

        $exe = Join-Path $installDir 'workboard.exe'
        $reported = "$(& $exe --version)"
        if ($LASTEXITCODE -ne 0 -or -not ($reported -match '^workboard (\S+)')) {
            throw "The installed binary did not run: $exe --version printed '$reported'."
        }
        $installed = $Matches[1]
        $receipt = [ordered]@{
            channel = 'script'
            version = $installed
            installedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
            installDir = $installDir
        }
        [IO.File]::WriteAllText((Join-Path $installDir 'install-receipt.json'), ($receipt | ConvertTo-Json) + "`n")
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host "WorkBoard $installed installed to $installDir"
    if ($NoModifyPath -or $env:WORKBOARD_NO_MODIFY_PATH -eq '1') {
        Write-Host "PATH not modified; add $installDir to PATH to run workboard."
    } else {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        try {
            $userPath = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if (($userPath -split ';') -notcontains $installDir) {
                $entries = @($userPath.TrimEnd(';'), $installDir) | Where-Object { $_ }
                $key.SetValue('Path', ($entries -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
                # Setting (then clearing) a user variable through .NET broadcasts WM_SETTINGCHANGE,
                # so terminals opened from now on see the new PATH.
                $marker = "WORKBOARD_INSTALL_$([guid]::NewGuid().ToString('N'))"
                [Environment]::SetEnvironmentVariable($marker, '1', 'User')
                [Environment]::SetEnvironmentVariable($marker, '', 'User')
                Write-Host "Added $installDir to your user PATH (open a new terminal to pick it up)."
            }
        } finally {
            $key.Close()
        }
        if (($env:Path -split ';') -notcontains $installDir) { $env:Path = "$installDir;$env:Path" }
    }
    Write-Host 'Run: workboard setup'
}

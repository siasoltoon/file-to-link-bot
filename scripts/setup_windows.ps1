$ErrorActionPreference = "Stop"

Write-Host "Preparing Windows environment for file-to-link bot..."

function Test-CommandExists {
    param([string]$Command)
    return $null -ne (Get-Command $Command -ErrorAction SilentlyContinue)
}

function Ensure-Chocolatey {
    if (Test-CommandExists "choco") {
        Write-Host "Chocolatey is already installed."
        return
    }

    Write-Host "Installing Chocolatey..."
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

    $chocoPath = "$env:ChocolateyInstall\bin"
    if (Test-Path $chocoPath) {
        $env:Path = "$chocoPath;$env:Path"
    }

    if (-not (Test-CommandExists "choco")) {
        throw "Chocolatey installation failed."
    }
}

function Ensure-ChocoPackage {
    param(
        [Parameter(Mandatory=$true)][string]$Package,
        [string]$DisplayName = $Package
    )

    Write-Host "Checking application: $DisplayName ($Package)"
    & choco list --local-only --exact $Package --limit-output | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $installed = & choco list --local-only --exact $Package --limit-output
        if ($installed -match "^$([regex]::Escape($Package))\|") {
            Write-Host "Already installed: $DisplayName"
            return
        }
    }

    Write-Host "Installing: $DisplayName"
    & choco install $Package --yes --no-progress --limit-output
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install $DisplayName ($Package)."
    }
    Write-Host "Installed: $DisplayName"
}

Ensure-Chocolatey

# User applications / utilities for every fresh Windows VPS.
# These are intentionally installed idempotently: existing applications are reused.
$apps = @(
    @{ Package = "telegram"; DisplayName = "Telegram Desktop" },
    @{ Package = "internet-download-manager"; DisplayName = "Internet Download Manager (IDM)" },
    @{ Package = "googlechrome"; DisplayName = "Google Chrome" },
    @{ Package = "7zip"; DisplayName = "7-Zip" },
    @{ Package = "vlc"; DisplayName = "VLC Media Player" },
    @{ Package = "git"; DisplayName = "Git" }
)

foreach ($app in $apps) {
    Ensure-ChocoPackage -Package $app.Package -DisplayName $app.DisplayName
}

# Tailscale is installed by the RDP workflow itself because it needs the
# TAILSCALE_AUTHKEY secret and must be authenticated after installation.
Write-Host "Tailscale installation/authentication remains in the RDP workflow."

$python = (& py -3.13 -c "import sys; print(sys.executable)").Trim()
if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path $python)) {
    throw "Python 3.13 executable was not found."
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

New-Item -ItemType Directory -Force -Path ".\data" | Out-Null

Write-Host "Running Telegram network diagnostics..."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\network_diagnostics.ps1"
if ($LASTEXITCODE -ne 0) { throw "Network diagnostics failed." }

Write-Host "Windows bot environment and applications are ready."

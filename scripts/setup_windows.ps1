$ErrorActionPreference = "Stop"

Write-Host "Preparing Windows environment for file-to-link bot..."

$python = (& py -3.13 -c "import sys; print(sys.executable)").Trim()
if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path $python)) {
    throw "Python 3.13 executable was not found."
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

New-Item -ItemType Directory -Force -Path ".\data" | Out-Null

Write-Host "Windows bot environment is ready."

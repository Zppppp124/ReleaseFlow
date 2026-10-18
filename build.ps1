$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "assets\ReleaseFlow.ico")) {
    python -c "from releaseflow.icons import ensure_ico; ensure_ico(__import__('pathlib').Path('assets/ReleaseFlow.ico'))"
}

$env:PYTHONPATH = $root
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }

pyinstaller --noconfirm --clean ReleaseFlow.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\ReleaseFlow\ReleaseFlow.exe"
if (-not (Test-Path $exe)) { throw "ReleaseFlow.exe was not built" }

Write-Host ""
Write-Host "Built: $exe" -ForegroundColor Green

param(
    [string]$RequirementsFile = "requirements-release.txt"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$requirementsPath = Join-Path $projectRoot $RequirementsFile
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Requirements file not found: $requirementsPath"
}

$iconPath = Join-Path $projectRoot "icon.png"
if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
    throw "GUI icon not found: $iconPath"
}

$licensePath = Join-Path $projectRoot "LICENSE"
if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf)) {
    throw "Project license not found: $licensePath"
}

$noticesPath = Join-Path $projectRoot "THIRD_PARTY_NOTICES.md"
if (-not (Test-Path -LiteralPath $noticesPath -PathType Leaf)) {
    throw "Third-party notices not found: $noticesPath"
}

Write-Host "Installing portable GUI build requirements from $RequirementsFile..." -ForegroundColor Cyan
python -m pip install -r $requirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install portable GUI build requirements."
}

$pyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--onedir"
    "--windowed"
    "--python-option"
    "u"
    "--name"
    "H145-Local-OSM"
    "--icon"
    "icon.png"
    "--collect-all"
    "osmium"
    "--add-data"
    "tools/osmium:tools/osmium"
    "--add-data"
    "icon.png:."
    "h145_local_osm_gui.py"
)

Write-Host "Building H145 Local OSM portable application..." -ForegroundColor Cyan
python -I (Join-Path $PSScriptRoot "build_portable_gui.py") @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$portableRoot = Join-Path $projectRoot "dist\H145-Local-OSM"
$exe = Join-Path $portableRoot "H145-Local-OSM.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Expected packaged executable was not produced: $exe"
}

$packagedIcon = Get-ChildItem -Path $portableRoot -Filter "icon.png" -File -Recurse | Select-Object -First 1
if ($null -eq $packagedIcon) {
    throw "Packaged GUI icon was not included in the portable application."
}

Copy-Item -LiteralPath $licensePath -Destination (Join-Path $portableRoot "LICENSE") -Force
Copy-Item -LiteralPath $noticesPath -Destination (Join-Path $portableRoot "THIRD_PARTY_NOTICES.md") -Force

foreach ($directory in @("data", "downloads", "reports")) {
    New-Item -ItemType Directory -Force (Join-Path $portableRoot $directory) | Out-Null
}

Write-Host ""
Write-Host "Portable application created:" -ForegroundColor Green
Write-Host "  $portableRoot"
Write-Host ""
Write-Host "The application stores config.json beside the executable and keeps databases/downloads/reports inside this portable folder."
